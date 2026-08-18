// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title TradingVault
 * @notice Pooled ERC-4626-style vault (USDT) wrapping the OKX trading agent.
 *
 * Share accounting follows ERC-4626 semantics — deposits mint shares
 * proportional to current NAV, redemptions exchange shares for proportional
 * capital — with virtual shares to resist the classic donation attack.
 *
 * Three deliberate design choices:
 * 1. Limits enforced at execution. MIN_DEPOSIT and MAX_TVL revert INSIDE
 *    deposit(), not merely stored. This is the lesson from liquid-protocol-v1,
 *    whose Strategy stores minDeposit but never checks it at the join point;
 *    PrivateVault (private-vault-nox) is the proven pattern.
 * 2. Two-step withdrawals (request -> finalize -> expire), ported from
 *    PrivateVault. The price is taken BEFORE the burn so burning shares cannot
 *    inflate the share price and overpay this request at the expense of remaining
 *    shareholders.
 * 3. Settlement-window-only redemptions: finalizeWithdraw reverts while a
 *    funding-arb package is open (fundingPackageOpen). Forced close is out of
 *    scope; settlement docs must be honest about worst-case (window open plus
 *    one period) and the UI flags it.
 *
 * NAV is operator-attested: the agent EOA (onlyAgent) reports its real OKX
 * balance plus vault reserve through attestTotalAssets, with a deployer-set
 * timelock and a per-attestation delta cap (MAX_ATTESTATION_DELTA_BPS).
 * This is the v1 reconciliation model (mechanism 1 in the design doc) — the
 * depositor UI labels it "value reported by the operator".
 *
 * Agent controls:
 * - Two-step transfer: requestAgentTransfer (agent) -> executeAgentTransfer
 *   (after 48h timelock). Per-tx cap at 10% of MAX_TVL.
 * - Agent rotation: proposeAgent (owner) -> acceptAgent (proposed, after 72h).
 * - expireWithdrawal is self-serve: the requesting user can expire their own
 *   request after the deadline (previously owner-only).
 */
contract TradingVault {
    /* ========== ERC20 SHARE STATE ========== */

    string public constant name = "Trading Vault Share";
    string public constant symbol = "TVLT";
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    /* ========== VAULT STATE ========== */

    address public immutable owner;
    address public agent;
    IERC20 public immutable asset;
    uint8 public immutable assetDecimals;

    /// @notice Minimum deposit, in asset minimal units. Enforced inside deposit().
    uint256 public immutable MIN_DEPOSIT;
    /// @notice NAV cap, in asset minimal units. Enforced inside deposit() and attestTotalAssets().
    uint256 public immutable MAX_TVL;
    /// @notice Minimum time between operator NAV attestations.
    uint256 public immutable ATTEST_TIMELOCK;
    /// @notice Per-transfer cap: 10% of MAX_TVL (set in constructor).
    uint256 public immutable AGENT_TRANSFER_CAP;
    /// @notice Timelock for executing an agent transfer request.
    uint256 public immutable AGENT_TRANSFER_TIMELOCK;
    /// @notice Timelock for agent rotation acceptance.
    uint256 public immutable AGENT_ROTATION_TIMELOCK;
    /// @notice Max NAV change per attestation in basis points (1000 = 10%).
    uint256 public immutable MAX_ATTESTATION_DELTA_BPS;

    uint256 private constant WITHDRAWAL_DEADLINE = 3 days;
    uint256 private constant WITHDRAWAL_RATE_LIMIT = 1 hours;
    uint256 private constant _VIRTUAL_ASSETS = 1e6;
    uint256 private constant _VIRTUAL_SHARES = 1e6;
    uint256 private constant _SHARE_PRICE_PRECISION = 1e18;
    uint256 private constant _BPS_DENOMINATOR = 10_000;

    struct WithdrawalRequest {
        uint256 shares;
        uint256 usdtOut;
        address owner;
        uint256 deadline;
        bool finalized;
    }

    struct AgentTransferRequest {
        uint256 amount;
        uint256 requestedAt;
        bool pending;
    }

    uint256 public totalAssetsPriced;
    uint256 public pendingReserved;
    uint256 public withdrawalCount;
    uint256 public lastAttestation;
    bool public fundingPackageOpen;
    uint256 private _reentrancyStatus = 1;

    mapping(address => uint256) public lastWithdrawalRequest;
    mapping(uint256 => WithdrawalRequest) public withdrawalRequests;

    address public proposedAgent;
    uint256 public agentProposedAt;
    uint256 public agentTransferCount;
    mapping(uint256 => AgentTransferRequest) public agentTransferRequests;

    /* ========== EVENTS ========== */

    event Deposit(address indexed account, uint256 amount, uint256 shares);
    event WithdrawalRequested(uint256 indexed requestId, address indexed account, uint256 shares);
    event WithdrawalFinalized(uint256 indexed requestId, address indexed account, uint256 amount);
    event WithdrawalExpired(uint256 indexed requestId, address indexed account);
    event NavAttested(uint256 totalAssets, uint256 timestamp);
    event FundingPackageOpenChanged(bool open, uint256 timestamp);
    event AgentTransferRequested(uint256 indexed requestId, uint256 amount);
    event AgentTransferExecuted(uint256 indexed requestId, uint256 amount);
    event AgentTransferCancelled(uint256 indexed requestId);
    event AgentProposed(address indexed newAgent, uint256 proposedAt);
    event AgentRotated(address indexed newAgent, uint256 rotatedAt);
    event AgentRotationAborted(address indexed formerAgent);
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    /* ========== ERRORS ========== */

    error OnlyOwner();
    error OnlyAgent();
    error ETHNotAccepted();
    error DepositTooSmall();
    error MaxTvlExceeded();
    error InvalidAmount();
    error InvalidAgent();
    error AlreadyFinalized();
    error NotWithdrawalOwner();
    error WithdrawalDeadlinePassed();
    error WithdrawalNotExpired();
    error WithdrawalRateLimited();
    error NothingToExpire();
    error ReentrantCall();
    error TransferFailed();
    error SettlementWindowClosed();
    error AttestationTimelocked();
    error ReservedExceedsNav();
    error ReservedExceedsBalance();
    error AgentTransferExceedsCap();
    error AgentTransferNotPending();
    error AgentTransferTimelocked();
    error NoAgentProposed();
    error NotProposedAgent();
    error RotationTimelocked();
    error AttestationDeltaTooLarge();

    /* ========== MODIFIERS ========== */

    modifier onlyOwner() {
        if (msg.sender != owner) revert OnlyOwner();
        _;
    }

    modifier onlyAgent() {
        if (msg.sender != agent) revert OnlyAgent();
        _;
    }

    modifier nonReentrant() {
        if (_reentrancyStatus != 1) revert ReentrantCall();
        _reentrancyStatus = 2;
        _;
        _reentrancyStatus = 1;
    }

    constructor(
        IERC20 _asset,
        address _agent,
        uint256 _minDeposit,
        uint256 _maxTvl,
        uint256 _attestTimelock,
        uint256 _maxAttestationDeltaBps
    ) {
        owner = msg.sender;
        agent = _agent;
        asset = _asset;
        assetDecimals = _asset.decimals();
        MIN_DEPOSIT = _minDeposit;
        MAX_TVL = _maxTvl;
        ATTEST_TIMELOCK = _attestTimelock;
        AGENT_TRANSFER_CAP = _maxTvl / 10;
        AGENT_TRANSFER_TIMELOCK = 48 hours;
        AGENT_ROTATION_TIMELOCK = 72 hours;
        MAX_ATTESTATION_DELTA_BPS = _maxAttestationDeltaBps;
    }

    receive() external payable {
        revert ETHNotAccepted();
    }

    /* ========== ERC20 ========== */

    function _mint(address to, uint256 amount) internal {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function _burn(address from, uint256 amount) internal {
        balanceOf[from] -= amount;
        totalSupply -= amount;
        emit Transfer(from, address(0), amount);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        emit Transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - amount;
        }
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
        return true;
    }

    /* ========== VIEWS ========== */

    function _netAssets() internal view returns (uint256) {
        return totalAssetsPriced - pendingReserved;
    }

    function totalAssets() public view returns (uint256) {
        return totalAssetsPriced;
    }

    function sharePriceAsset() public view returns (uint256) {
        uint256 supply = totalSupply + _VIRTUAL_SHARES;
        return (_netAssets() + _VIRTUAL_ASSETS) * _SHARE_PRICE_PRECISION / supply;
    }

    function convertToShares(uint256 assets) public view returns (uint256) {
        uint256 supply = totalSupply + _VIRTUAL_SHARES;
        return assets * supply / (_netAssets() + _VIRTUAL_ASSETS);
    }

    function convertToAssets(uint256 shares) public view returns (uint256) {
        uint256 supply = totalSupply + _VIRTUAL_SHARES;
        return shares * (_netAssets() + _VIRTUAL_ASSETS) / supply;
    }

    function settlementOpen() public view returns (bool) {
        return !fundingPackageOpen;
    }

    /* ========== DEPOSITS ========== */

    function deposit(uint256 amount) external nonReentrant returns (uint256 shares) {
        if (amount < MIN_DEPOSIT) revert DepositTooSmall();
        if (totalAssetsPriced + amount > MAX_TVL) revert MaxTvlExceeded();

        shares = convertToShares(amount);
        if (shares == 0) revert InvalidAmount();

        if (!asset.transferFrom(msg.sender, address(this), amount)) revert TransferFailed();

        _mint(msg.sender, shares);
        totalAssetsPriced += amount;

        emit Deposit(msg.sender, amount, shares);
    }

    /* ========== TWO-STEP WITHDRAWALS ========== */

    function requestWithdraw(uint256 shares) external nonReentrant returns (uint256 requestId) {
        if (shares == 0) revert InvalidAmount();
        if (shares > balanceOf[msg.sender]) revert InvalidAmount();
        if (block.timestamp < lastWithdrawalRequest[msg.sender] + WITHDRAWAL_RATE_LIMIT) {
            revert WithdrawalRateLimited();
        }

        uint256 usdtOut = convertToAssets(shares);
        _burn(msg.sender, shares);
        pendingReserved += usdtOut;
        lastWithdrawalRequest[msg.sender] = block.timestamp;
        withdrawalCount += 1;
        requestId = withdrawalCount;
        withdrawalRequests[requestId] = WithdrawalRequest({
            shares: shares,
            usdtOut: usdtOut,
            owner: msg.sender,
            deadline: block.timestamp + WITHDRAWAL_DEADLINE,
            finalized: false
        });

        emit WithdrawalRequested(requestId, msg.sender, shares);
    }

    function finalizeWithdraw(uint256 requestId) external nonReentrant {
        WithdrawalRequest storage req = withdrawalRequests[requestId];
        if (req.finalized) revert AlreadyFinalized();
        if (msg.sender != req.owner) revert NotWithdrawalOwner();
        if (block.timestamp > req.deadline) revert WithdrawalDeadlinePassed();
        if (fundingPackageOpen) revert SettlementWindowClosed();

        req.finalized = true;
        pendingReserved -= req.usdtOut;
        totalAssetsPriced -= req.usdtOut;

        if (!asset.transfer(req.owner, req.usdtOut)) revert TransferFailed();

        emit WithdrawalFinalized(requestId, req.owner, req.usdtOut);
    }

    /// @notice Reclaim a stale request after its deadline, re-minting shares.
    /// @dev Owner or requesting user can expire. Guarantees a request can never
    ///      brick: if the agent never opens a settlement window before the
    ///      deadline, the shares are restored instead of silently lost.
    function expireWithdrawal(uint256 requestId) external nonReentrant {
        WithdrawalRequest storage req = withdrawalRequests[requestId];
        if (req.finalized) revert AlreadyFinalized();
        if (block.timestamp < req.deadline) revert WithdrawalNotExpired();
        if (req.owner == address(0)) revert NothingToExpire();
        if (msg.sender != owner && msg.sender != req.owner) revert OnlyOwner();

        req.finalized = true;
        pendingReserved -= req.usdtOut;
        _mint(req.owner, req.shares);

        emit WithdrawalExpired(requestId, req.owner);
    }

    /* ========== OPERATOR ATTESTATION (reconciliation, mechanism 1) ========== */

    /// @notice Agent reports the pool's real value (OKX balance + vault reserve).
    /// @dev Timelocked; capped at MAX_TVL and at MAX_ATTESTATION_DELTA_BPS
    ///      change per attestation. This is the v1 reconciliation model.
    function attestTotalAssets(uint256 newTotalAssets) external onlyAgent {
        if (newTotalAssets > MAX_TVL) revert MaxTvlExceeded();
        if (newTotalAssets < pendingReserved) revert ReservedExceedsNav();
        if (block.timestamp < lastAttestation + ATTEST_TIMELOCK) revert AttestationTimelocked();

        if (totalAssetsPriced > 0) {
            uint256 prior = totalAssetsPriced;
            uint256 delta = newTotalAssets > prior
                ? newTotalAssets - prior
                : prior - newTotalAssets;
            uint256 maxDelta = prior * MAX_ATTESTATION_DELTA_BPS / _BPS_DENOMINATOR;
            if (delta > maxDelta) revert AttestationDeltaTooLarge();
        }

        lastAttestation = block.timestamp;
        totalAssetsPriced = newTotalAssets;

        emit NavAttested(newTotalAssets, block.timestamp);
    }

    /// @notice Agent marks a funding-arb package open/closed.
    function setFundingPackageOpen(bool open) external onlyAgent {
        fundingPackageOpen = open;
        emit FundingPackageOpenChanged(open, block.timestamp);
    }

    /* ========== AGENT TRANSFER (two-step, timelocked) ========== */

    /// @notice Step 1: agent requests a capital transfer (periodic provisioning).
    /// @dev Per-tx cap at 10% of MAX_TVL. Requires a 48h timelock before execution.
    function requestAgentTransfer(uint256 amount) external onlyAgent nonReentrant returns (uint256 requestId) {
        if (amount == 0) revert InvalidAmount();
        if (amount > AGENT_TRANSFER_CAP) revert AgentTransferExceedsCap();
        agentTransferCount += 1;
        requestId = agentTransferCount;
        agentTransferRequests[requestId] = AgentTransferRequest({
            amount: amount,
            requestedAt: block.timestamp,
            pending: true
        });
        emit AgentTransferRequested(requestId, amount);
    }

    /// @notice Step 2: execute a pending transfer after the timelock elapses.
    function executeAgentTransfer(uint256 requestId) external onlyAgent nonReentrant {
        AgentTransferRequest storage req = agentTransferRequests[requestId];
        if (!req.pending) revert AgentTransferNotPending();
        if (block.timestamp < req.requestedAt + AGENT_TRANSFER_TIMELOCK) revert AgentTransferTimelocked();
        if (asset.balanceOf(address(this)) < req.amount + pendingReserved) revert ReservedExceedsBalance();

        req.pending = false;
        if (!asset.transfer(agent, req.amount)) revert TransferFailed();
        emit AgentTransferExecuted(requestId, req.amount);
    }

    /// @notice Owner cancels a pending agent transfer request.
    function cancelAgentTransfer(uint256 requestId) external onlyOwner nonReentrant {
        AgentTransferRequest storage req = agentTransferRequests[requestId];
        if (!req.pending) revert AgentTransferNotPending();
        req.pending = false;
        emit AgentTransferCancelled(requestId);
    }

    /* ========== AGENT ROTATION ========== */

    /// @notice Owner proposes a new agent address. Must wait AGENT_ROTATION_TIMELOCK
    ///         before the proposed agent can accept.
    function proposeAgent(address newAgent) external onlyOwner {
        if (newAgent == address(0)) revert InvalidAgent();
        proposedAgent = newAgent;
        agentProposedAt = block.timestamp;
        emit AgentProposed(newAgent, block.timestamp);
    }

    /// @notice Proposed agent accepts the role after the timelock.
    function acceptAgent() external nonReentrant {
        if (proposedAgent == address(0)) revert NoAgentProposed();
        if (msg.sender != proposedAgent) revert NotProposedAgent();
        if (block.timestamp < agentProposedAt + AGENT_ROTATION_TIMELOCK) revert RotationTimelocked();

        agent = proposedAgent;
        delete proposedAgent;
        agentProposedAt = 0;
        emit AgentRotated(msg.sender, block.timestamp);
    }

    /// @notice Owner aborts a pending agent rotation proposal.
    function abortAgentRotation() external onlyOwner {
        if (proposedAgent == address(0)) revert NoAgentProposed();
        emit AgentRotationAborted(proposedAgent);
        delete proposedAgent;
        agentProposedAt = 0;
    }
}

/// @notice Minimal ERC-20 interface (USDT and mocks implement these).
interface IERC20 {
    function decimals() external view returns (uint8);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}
