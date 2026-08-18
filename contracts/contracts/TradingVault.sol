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
 *    inflate the share price and overpay this request.
 * 3. Settlement-window-only redemptions. finalizeWithdraw reverts while a
 *    funding-arb package is open (fundingPackageOpen). OKX perps settle
 *    funding every 8h, so the worst-case wait is one settlement window.
 *
 * NAV is operator-attested: the agent EOA (onlyAgent) reports its real OKX
 * balance plus vault reserve through attestTotalAssets, with a deployer-set
 * timelock. This is the v1 reconciliation model (mechanism 1 in the design
 * doc) — the depositor UI labels it "value reported by the operator".
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
    address public immutable agent;
    IERC20 public immutable asset;
    uint8 public immutable assetDecimals;

    /// @notice Minimum deposit, in asset minimal units. Enforced inside deposit().
    uint256 public immutable MIN_DEPOSIT;
    /// @notice NAV cap, in asset minimal units. Enforced inside deposit() and attestTotalAssets().
    uint256 public immutable MAX_TVL;
    /// @notice Minimum time between operator NAV attestations.
    uint256 public immutable ATTEST_TIMELOCK;

    uint256 private constant WITHDRAWAL_DEADLINE = 3 days;
    uint256 private constant WITHDRAWAL_RATE_LIMIT = 1 hours;
    uint256 private constant _VIRTUAL_ASSETS = 1e6; // asset minimal units (1 USDT)
    uint256 private constant _VIRTUAL_SHARES = 1e6; // share units
    uint256 private constant _SHARE_PRICE_PRECISION = 1e18;

    struct WithdrawalRequest {
        uint256 shares;  // shares burned at request time
        uint256 usdtOut; // reserved asset minimal units, priced at request time
        address owner;
        uint256 deadline;
        bool finalized;
    }

    /// @notice Operator-attested NAV in asset minimal units (vault reserve + OKX balance).
    uint256 public totalAssetsPriced;
    /// @notice Asset minimal units reserved for outstanding withdrawal requests.
    /// @dev Excluded from share-pricing so a request in flight (shares burned,
    ///      value still in the pool) cannot inflate convertToShares/convertToAssets
    ///      for subsequent deposits or requests.
    uint256 public pendingReserved;
    uint256 public withdrawalCount;
    uint256 public lastAttestation;
    bool public fundingPackageOpen;
    uint256 private _reentrancyStatus = 1;

    mapping(address => uint256) public lastWithdrawalRequest;
    mapping(uint256 => WithdrawalRequest) public withdrawalRequests;

    /* ========== EVENTS ========== */

    event Deposit(address indexed account, uint256 amount, uint256 shares);
    event WithdrawalRequested(uint256 indexed requestId, address indexed account, uint256 shares);
    event WithdrawalFinalized(uint256 indexed requestId, address indexed account, uint256 amount);
    event WithdrawalExpired(uint256 indexed requestId, address indexed account);
    event NavAttested(uint256 totalAssets, uint256 timestamp);
    event FundingPackageOpenChanged(bool open, uint256 timestamp);
    event AgentWithdrawal(uint256 amount);
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    /* ========== ERRORS ========== */

    error OnlyOwner();
    error OnlyAgent();
    error ETHNotAccepted();
    error DepositTooSmall();
    error MaxTvlExceeded();
    error InvalidAmount();
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
        uint256 _attestTimelock
    ) {
        owner = msg.sender;
        agent = _agent;
        asset = _asset;
        assetDecimals = _asset.decimals();
        MIN_DEPOSIT = _minDeposit;
        MAX_TVL = _maxTvl;
        ATTEST_TIMELOCK = _attestTimelock;
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

    /// @notice Pool value available to live shares (attested NAV minus assets
    ///         already reserved for pending withdrawal requests).
    function _netAssets() internal view returns (uint256) {
        return totalAssetsPriced - pendingReserved;
    }

    /// @notice Total pool value in asset minimal units (operator-attested).
    function totalAssets() public view returns (uint256) {
        return totalAssetsPriced;
    }

    /// @notice Share price in asset minimal units, 18-decimal scaled.
    /// @dev Includes the virtual offset so the first deposit starts at ~1:1.
    ///      Quotes against net assets (pending reserves excluded) so a pending
    ///      request cannot inflate the price for new deposits.
    function sharePriceAsset() public view returns (uint256) {
        uint256 supply = totalSupply + _VIRTUAL_SHARES;
        return (_netAssets() + _VIRTUAL_ASSETS) * _SHARE_PRICE_PRECISION / supply;
    }

    /// @notice ERC-4626 style conversion: asset amount -> share amount.
    function convertToShares(uint256 assets) public view returns (uint256) {
        uint256 supply = totalSupply + _VIRTUAL_SHARES;
        return assets * supply / (_netAssets() + _VIRTUAL_ASSETS);
    }

    /// @notice ERC-4626 style conversion: share amount -> asset amount.
    function convertToAssets(uint256 shares) public view returns (uint256) {
        uint256 supply = totalSupply + _VIRTUAL_SHARES;
        return shares * (_netAssets() + _VIRTUAL_ASSETS) / supply;
    }

    /// @notice Settlement window is open when no funding-arb package is running.
    function settlementOpen() public view returns (bool) {
        return !fundingPackageOpen;
    }

    /* ========== DEPOSITS ========== */

    /// @notice Deposit USDT and mint shares at the current NAV.
    /// @dev MIN_DEPOSIT and MAX_TVL are enforced here, at execution, not stored inertly.
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

    /// @notice Step 1: burn shares and open a withdrawal request.
    /// @dev Priced BEFORE the burn so burning cannot inflate the share price
    ///      and overpay this request at the expense of remaining shareholders.
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

    /// @notice Step 2: finalize a request and receive the reserved USDT.
    /// @dev Only between funding-arb packages (settlement-window model (b)).
    ///      Reverts while a package is open — the depositor waits one window.
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

    /// @notice Owner-only: reclaim a stale request after its deadline, re-minting shares.
    /// @dev Guarantees a request can never brick-pay a depositor: if the agent
    ///      never opens a settlement window before the deadline, the shares are
    ///      restored instead of silently lost.
    function expireWithdrawal(uint256 requestId) external nonReentrant onlyOwner {
        WithdrawalRequest storage req = withdrawalRequests[requestId];
        if (req.finalized) revert AlreadyFinalized();
        if (block.timestamp < req.deadline) revert WithdrawalNotExpired();
        if (req.owner == address(0)) revert NothingToExpire();

        req.finalized = true;
        pendingReserved -= req.usdtOut;
        _mint(req.owner, req.shares);

        emit WithdrawalExpired(requestId, req.owner);
    }

    /* ========== OPERATOR ATTESTATION (reconciliation, mechanism 1) ========== */

    /// @notice Agent reports the pool's real value (OKX balance + vault reserve).
    /// @dev Timelocked; capped at MAX_TVL. This is the v1 reconciliation model.
    function attestTotalAssets(uint256 newTotalAssets) external onlyAgent {
        if (newTotalAssets > MAX_TVL) revert MaxTvlExceeded();
        if (newTotalAssets < pendingReserved) revert ReservedExceedsNav();
        if (block.timestamp < lastAttestation + ATTEST_TIMELOCK) revert AttestationTimelocked();

        lastAttestation = block.timestamp;
        totalAssetsPriced = newTotalAssets;

        emit NavAttested(newTotalAssets, block.timestamp);
    }

    /// @notice Agent marks a funding-arb package open/closed. While open,
    ///         finalizeWithdraw reverts (settlement-window-only redemption).
    function setFundingPackageOpen(bool open) external onlyAgent {
        fundingPackageOpen = open;
        emit FundingPackageOpenChanged(open, block.timestamp);
    }

    /// @notice Agent moves reserve USDT to its OKX funding address.
    /// @dev The money stays in the pool (deployed on OKX); NAV is unchanged and
    ///      reconciled later via attestTotalAssets. Agent is the only mover.
    function transferToAgent(uint256 amount) external onlyAgent {
        if (amount == 0) revert InvalidAmount();
        // The vault's physical USDT must keep covering every pending request
        // after the agent moves money out; otherwise withdrawals would stall
        // until their 3-day expiry re-mints shares.
        if (asset.balanceOf(address(this)) < amount + pendingReserved) revert ReservedExceedsBalance();
        if (!asset.transfer(agent, amount)) revert TransferFailed();
        emit AgentWithdrawal(amount);
    }
}

/// @notice Minimal ERC-20 interface (USDT and mocks implement these).
interface IERC20 {
    function decimals() external view returns (uint8);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}
