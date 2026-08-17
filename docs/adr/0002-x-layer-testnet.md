# ADR 0002: X Layer Testnet (chainId 1952) as the audit chain

## Status

Accepted (2026-08).

## Context

The autonomous agent logs every trade decision to an immutable `TradeAuditTrail`
contract *before* execution, so the audit trail is verifiable and the agent
cannot bypass its own risk controls. We needed a low-cost EVM chain with OKX
ecosystem alignment.

Candidates:

- **Arbitrum / Base** — mature tooling, but no native OKX account/key alignment
  and higher L2 data-availability cost for a high-frequency log.
- **X Layer (OKX L2, chainId 1952 testnet)** — direct OKX ecosystem fit, cheap
  testnet gas, and the same EOA/key model as the trading account, simplifying
  the "agent address == signer" invariant the contract enforces (`onlyAgent`,
  `msg.sender == tx.origin`).

## Decision

Deploy `TradeAuditTrail.sol` to **X Layer Testnet, chainId 1952**. The chain id
is the single source of truth for every component that builds a signed tx:
`src/audit_logger.py` (default), `src/main.py` (`XLAYER_CHAIN_ID` default), and
`contracts/scripts/deploy.py` (`CHAIN_ID`).

## Consequences

- Any component signing for a different chain id will have its transactions
  revert on submission — a guard, not a footgun. The default mismatch (was `195`
  in `audit_logger.py`) was a latent bug; it is now `1952` everywhere and covered
  by `tests/test_chain_id_consistency.py`.
- Mainnet migration is a config change (`XLAYER_CHAIN_ID` + redeploy + risk-param
  set), not a code change.
