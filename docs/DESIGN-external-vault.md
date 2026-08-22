# External-User Strategy: Pooled Vault + Depositor UI

**Status:** DESIGN — proposal for review, no code written.
**Scope:** `AuditTrailTrader` (AuditTrail Trader). This is *not* the
`sibling private-vault-nox` FXRP vault — that project solves XRP-on-Flare
privacy. This document is about wrapping the existing OKX trading agent in a
pooled capital vault so external users can deposit, watch, and withdraw.

---

## 1. The core insight: pooled vault, trading system unchanged

Everything the agent already does — risk gate, curator profiles, Kelly sizing,
regime throttle, the funding-arb package machinery — trades **one pot of
capital**. The agent holds one OKX account and runs one decision loop
(`src/agent.py::run_trading_cycle`).

In a **pooled-vault** model, external deposits just make that pot bigger. The
agent still trades exactly one account, exactly as it does today. This is the
whole reason pooled beats per-depositor delegation: **no trading logic becomes
multi-tenant-aware.** Two new things exist, and only two:

1. A **vault contract** (custody + share accounting).
2. A **genuinely different, much simpler frontend** (depositor comprehension).

Nothing in `src/agent.py`, `src/execution.py` (RiskGate), `src/curator.py`, or
the funding-arb machinery changes. The pot just gets bigger.

---

## 2. Vault contract — ERC-4626 style, enforced limits, honest redemption

### 2.1 Shape

Port the accounting model from `contracts/PrivateVault.sol` (sibling
`private-vault-nox`), which already encodes the two hard-won lessons:

- **Share math is standard ERC-4626, not bespoke.** Deposit → mint shares
  proportional to current value; share price rises as trading gains accrue;
  redeem shares for a proportional withdrawal. Don't reinvent share math.
- **Limits are enforced at the point of execution, not just declared.**
  `PrivateVault.sol:184` reverts `DepositTooSmall()` *inside* `deposit()`,
  not merely as a stored field. This is a deliberate deviation from the
  source MVP where `minDeposit` was declared but never checked at the join
  point. Any limit this vault has must follow the same discipline:
  verify at execution, not declaration.

### 2.2 New contract: `TradingVault.sol`

Deployed on X Layer alongside `TradeAuditTrail.sol` (same chain, chainId 1952,
see ADR-0002). `ERC4626`-based.

| Surface | Spec |
|---|---|
| Asset | USDT (the agent's actual trading currency) |
| Deposit | `deposit(amount)` → shares. `MIN_DEPOSIT` enforced **inside** `deposit()`, not just stored. `MAX_TVL` enforced inside `deposit()` too — a cap that matches the validation gate's proven capacity (see §5.2), so the agent never trades beyond what walk-forward/PBO/Calmar cleared |
| Share price | `totalAssets / totalSupply` (standard ERC-4626, virtual shares to resist donation attacks) |
| Withdraw | Two-step, ported from `PrivateVault.sol`: `requestWithdraw(shares)` (burn + price at burn time, `withdrawalRequests[reqId]`), then `finalizeWithdraw(reqId)` (transfer reserved USDT). Rate-limited (1/hr), deadline, owner-expiry of stale requests |
| Yield | No `injectYield`-style owner mint. Gains flow in through the agent's account, reconciled to `totalAssets` by a verifiable off-chain/on-chain bridge (§4.2) |
| Access | `onlyAgent`-style guard so the *agent* (same EOA that signs `TradeAuditTrail` decisions) is the only mover of the trading sub-account |

### 2.3 Withdrawal is the genuinely new hard problem — decide explicitly

If capital is actively deployed in open funding-arb packages, instant
redemption isn't always possible. Two honest options (pick one; do not
discover it live):

- **(a) Idle-capital buffer + queue.** A buffer sized to cover typical
  redemption demand; requests beyond the buffer queue until the next
  settlement window. Buffer size is a config, surfaced to depositors as
  "expected redemption delay."
- **(b) Settlement-window-only redemption.** Redemptions process only between
  packages. OKX perps settle funding every 8h (00:00 / 08:00 / 16:00 UTC), so
  the worst-case wait is one settlement window, not days. Simpler contract,
  no buffer sizing to get wrong.

**Recommendation for v1: (b).** It is simpler, the contract cannot be wrong
about liquidity in a way that brick-pays a depositor, and 8h is an honest,
communicable wait. Option (a) is a v1.1 upgrade if redemption demand makes
waiting painful — the two-step request model (`requestWithdraw` →
`finalizeWithdraw`) is already the right skeleton for both.

> **Design decision required:** (a) buffer+queue vs (b) settlement-window-only.
> This document proposes (b) for v1.

---

## 3. On-chain proof becomes the product pitch, not a feature

The audit trail that just shipped — `TradeAuditTrail.sol` with `packageId`,
`riskHash`, and per-agent onchain `dailyLoss`/`dailyTrades` counters — is the
differentiator. An external depositor can verify, without trusting the
dashboard:

- every trade decision (signed, before execution, `logDecision`),
- every funding-arb package (both legs under one `packageId`),
- every risk-gate rejection,
- the agent's cumulative daily loss (onchain counter, not dashboard copy).

That is a genuinely strong **"don't trust, verify"** story most DeFi vaults
can't make, and it is already ~90% built. The external UI's "view on-chain"
link is not a nicety — it is the safety argument.

---

## 4. Deposit/withdraw flow

### 4.1 Depositor flow (v1)

1. Connect wallet (EIP-1193, on X Layer chain 1952).
2. `TradingVault.deposit(usdtAmount)` → receive shares.
   - Reverts with a human-readable error if `< MIN_DEPOSIT` or would exceed
     `MAX_TVL` (both enforced in `deposit()`).
3. Position shown as shares → USDT at current share price (frontend reads
   `totalAssets()/totalSupply()` via `eth_call`).
4. **Withdraw:** `requestWithdraw(shares)` → burned, USDT reserved →
   `finalizeWithdraw(reqId)` after the settlement window.
   - If a funding-arb package is open and would be broken by withdrawing
     capital, the request queues until settlement (model (b)).

### 4.2 The reconciliation question (must be solved, out of scope for v1 contract)

`totalAssets` on the vault must equal what the agent actually holds in its OKX
trading account. This is the one place the pooled model has real teeth:
shares are priced against the agent's real balance, so P&L flows to depositors
proportionally.

Mechanisms, in increasing trust:

1. **Operator-attested balance** (v1): the agent reports `balance` +
   reconciliation every cycle; vault `totalAssets` is operator-set with a
   timelock + the onchain `recordExecution`/`dailyLoss` trail as the audit
   substrate. Weakest, fastest.
2. **Price-pinged assets** (v1.1): reconcile `totalAssets` against mark prices
   for the asset set each cycle, still operator-driven.
3. **Full onchain execution** (future): legs execute via a vault-controlled
   sub-account whose balance is provably the vault's. Strongest, largest
   lift — needs an onchain perp broker, which OKX's X Layer/x402 stack may or
   may not support.

> **Design decision required:** which reconciliation mechanism for v1. This
> document proposes (1) operator-attested with the existing audit trail as the
> substrate, clearly labeled as such in the depositor UI ("value reported by
> the operator, auditable on-chain").

---

## 5. The external UI is a different surface, not a simplified operator dashboard

### 5.1 The operator console stays exactly as-is

The current dashboard (`static/index.html`) — curator profile, Kelly mode,
regime throttle, PBO/Calmar, package state machine, kill switch — is right for
an operator and wrong for a depositor. **It is not simplified; it is kept
verbatim and operator-authenticated.** The kill switch, in particular, stays
**operator-only**: a depositor must never see a lever that halts trading —
showing one invites confusion about who controls their funds.

### 5.2 Depositor surface: three questions, answered fast

A depositor doesn't want to understand *how* it works. They want:

1. **Is my money safe?** One sentence, not nine checks:
   > *"Every trade passes a risk limit this agent cannot override, and you can
   > verify every trade on-chain."*
   Plus a `MIN_DEPOSIT`, a `MAX_TVL` cap, and the onchain address of
   `TradeAuditTrail.sol`.
2. **Is it working?** One number (return over time) and a simple equity curve.
   Not fees/slippage/leg-level detail.
3. **Can I get my money out?** Deposit and withdraw, and that's it.

### 5.3 Formal information architecture

```
/depositor  (public, no auth)
├── Hero            — one-sentence safety claim + "audit on-chain" link
├── Stats           — TVL, return (period), depositor count, share price
├── Deposit         — wallet connect → USDT → shares (MIN_DEPOSIT/MAX_TVL surfaced)
├── Withdraw        — request → finalize (settlement window shown)
├── My position     — shares, value in USDT, since-date
└── Verify          — links out to TradeAuditTrail.sol reads (decisions/packages/risk)

/operator  (existing dashboard, token-gated)
├── Everything today, unchanged
├── Kill switch     — operator-only, never surfaced on /depositor
└── (new) Vault ops — reconciliation, MIN_DEPOSIT/MAX_TVL config, buffer/window
```

Progressive disclosure, not simplification-by-deletion: depth exists behind
"view on-chain" for anyone who wants it, but curator profiles and regime
throttles never surface on `/depositor` by default.

### 5.4 Sequencing of the UI (build order)

1. **Wallet-connect for real** — the depositor page's connect button is wired
   to wagmi/viem (chain 1952), no longer a placeholder.
2. **Deposit/withdraw calling the real contract** — `TradingVault.deposit` /
   `requestWithdraw` / `finalizeWithdraw`; tx states surfaced honestly
   (pending → mined → reverted-with-reason).
3. **"My position" from real share balance** — reads `balanceOf(me)` and
   `totalAssets()/totalSupply()`, replacing the current placeholder dashes.
4. **Stats/curve from real data** — TVL, return, equity curve sourced from
   vault reads + the onchain trail.

---

## 6. Sequencing, if this becomes the actual plan

1. **`TradingVault.sol`** (ERC-4626 style, enforced `MIN_DEPOSIT`/`MAX_TVL` at
   `deposit()`, two-step withdrawal, settlement-window redemption) + full
   Hardhat/Foundry test suite, including: deposit reverts on
   `DepositTooSmall`, deposit reverts at `MAX_TVL`, share-price donation-attack
   resistance, two-step withdrawal, deadline expiry, rate limit. — *the real
   engineering lift.*
2. **Depositor surface v0** (static page, wallet connect real, deposit/withdraw
   against the deployed vault).
3. **Reconciliation v1** (operator-attested balance → `totalAssets`, with the
   audit trail as substrate).
4. **Nothing in `agent.py`, the risk gate, or the curator changes** — the pot
   just gets bigger.

---

## 7. Explicit non-goals for v1

- No multi-tenant trading logic. Per-depositor delegation, per-depositor risk
  params, and depositor-visible kill switches are all rejected.
- No `injectYield`-style owner yield minting (dilution and the C-4R-class
  unredeemable-shares bug that pattern caused in the sibling vault).
- No onchain execution for v1 (§4.2 mechanism 3 is a documented future state).
- No governance/DAO for strategy selection — the agent remains the sole,
  non-overridable-risk-gated trader.
