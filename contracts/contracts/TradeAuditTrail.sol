// SPDX-License-Identifier: MIT
pragma solidity ^0.8.30;

/**
 * @title TradeAuditTrail
 * @notice Non-overridable onchain audit log for autonomous AI trading decisions.
 *         Every trade signal, risk decision, and execution is recorded here.
 *         Agents CANNOT bypass this — the contract enforces logging before execution.
 *
 * Deployed on X Layer. Agents submit decisions via signed messages;
 * the risk engine validates them before they hit this log.
 *
 * Compile with: --via-ir (IR compiler for stack optimization)
 */
contract TradeAuditTrail {
    struct TradeDecision {
        bytes32 decisionId;
        address agent;
        string asset;
        string signal;
        string strategy;
        int256  confidence;
        uint256 entryPrice;
        uint256 sizeUsd;
        uint256 timestamp;
        bytes32 riskHash;
        bytes   signature;
        bool    executed;
        bool    isShort;
    }

    struct RiskParams {
        uint256 maxPositionSizeUsd;
        uint256 maxDailyLossUsd;
        uint256 maxLeverageBps;
        uint256 minConfidenceBps;
        bool    isMarketOrder;
    }

    struct ExecutionReceipt {
        bytes32 decisionId;
        uint256 fillPrice;
        uint256 fillSizeUsd;
        uint256 feeUsd;
        uint256 timestamp;
        bool    success;
    }

    // Packed struct for logDecision calldata to avoid stack-too-deep
    struct DecisionInput {
        bytes32 decisionId;
        string asset;
        string signal;
        string strategy;
        int256 confidence;
        uint256 entryPrice;
        uint256 sizeUsd;
        bytes32 riskHash;
        bytes signature;
        bool isShort;
    }

    TradeDecision[]   public decisions;
    ExecutionReceipt[] public executions;

    mapping(bytes32 => bool)        public decisionLogged;
    mapping(bytes32 => bool)        public decisionExecuted;
    mapping(address => uint256)     public dailyVolume;
    mapping(address => uint256)     public dailyLoss;
    mapping(address => uint256)     public dailyTrades;
    mapping(address => RiskParams)  public agentRiskParams;
    mapping(address => uint256)     public dailyBucket;   // current UTC-day bucket for this agent's counters
    mapping(address => bytes32)     public lastDecisionId;
    mapping(address => bool)        public killSwitchActive;
    mapping(address => string)      public killSwitchReason;

    event DecisionLogged(
        bytes32 indexed decisionId,
        address indexed agent,
        string asset,
        string signal,
        int256 confidence,
        uint256 sizeUsd,
        bytes32 riskHash
    );

    event TradeExecuted(
        bytes32 indexed decisionId,
        address indexed agent,
        uint256 fillPrice,
        uint256 fillSizeUsd,
        uint256 feeUsd,
        bool success
    );

    event RiskParamsUpdated(address indexed agent, RiskParams params);
    event KillSwitchActivated(address indexed agent, string reason, uint256 timestamp);
    event KillSwitchDeactivated(address indexed agent, uint256 timestamp);

    modifier onlyAgent() {
        require(msg.sender == tx.origin, "relayer calls not allowed");
        _;
    }

    constructor() {}

    /**
     * @notice Agent sets their non-overridable risk parameters.
     *         Once set, ALL trades from this agent must comply.
     *         Can only be tightened (never loosened) after initial set.
     */
    function setRiskParams(
        uint256 _maxPositionSizeUsd,
        uint256 _maxDailyLossUsd,
        uint256 _maxLeverageBps,
        uint256 _minConfidenceBps
    ) external {
        RiskParams storage p = agentRiskParams[msg.sender];

        if (p.maxPositionSizeUsd > 0) {
            require(_maxPositionSizeUsd <= p.maxPositionSizeUsd, "pos too high");
            require(_maxDailyLossUsd <= p.maxDailyLossUsd, "loss too high");
            require(_maxLeverageBps <= p.maxLeverageBps, "lev too high");
            require(_minConfidenceBps >= p.minConfidenceBps, "conf too low");
        }

        p.maxPositionSizeUsd = _maxPositionSizeUsd;
        p.maxDailyLossUsd = _maxDailyLossUsd;
        p.maxLeverageBps = _maxLeverageBps;
        p.minConfidenceBps = _minConfidenceBps;
        p.isMarketOrder = false;

        emit RiskParamsUpdated(msg.sender, p);
    }

    /**
     * @notice Halt all future logDecision calls from this agent immediately.
     *         Mirrors the off-chain RiskGate kill switch (src/execution.py) so
     *         the halt is enforced even if the off-chain process is compromised
     *         or bypassed — this is the non-overridable control, not a UI toggle.
     */
    function activateKillSwitch(string calldata reason) external onlyAgent {
        killSwitchActive[msg.sender] = true;
        killSwitchReason[msg.sender] = reason;
        emit KillSwitchActivated(msg.sender, reason, block.timestamp);
    }

    /**
     * @notice Resume trading. A deliberate, separate call — never automatic,
     *         same principle as the off-chain deactivate_kill_switch().
     */
    function deactivateKillSwitch() external onlyAgent {
        killSwitchActive[msg.sender] = false;
        killSwitchReason[msg.sender] = "";
        emit KillSwitchDeactivated(msg.sender, block.timestamp);
    }

    /**
     * @notice Log a trade decision BEFORE execution.
     *         Uses a struct to avoid stack-too-deep errors.
     */
    function logDecision(DecisionInput calldata input)
        external
        onlyAgent
        returns (bool)
    {
        require(!killSwitchActive[msg.sender], "KILL_SWITCH_ACTIVE");

        bytes32 decisionId = input.decisionId;
        require(!decisionLogged[decisionId], "decision already logged");

        bytes32 payloadHash = keccak256(abi.encodePacked(
            decisionId,
            msg.sender,
            input.asset,
            input.signal,
            input.strategy,
            input.confidence,
            input.entryPrice,
            input.sizeUsd,
            input.riskHash
        ));
        require(
            _isValidSignature(msg.sender, payloadHash, input.signature),
            "invalid signature"
        );

        // Risk gate: enforce agent's pre-set parameters
        RiskParams storage p = agentRiskParams[msg.sender];
        require(p.maxPositionSizeUsd > 0, "risk params not set");
        require(input.sizeUsd <= p.maxPositionSizeUsd, "EXCEEDS_MAX_POSITION_SIZE");
        require(uint256(input.confidence) >= p.minConfidenceBps, "BELOW_MIN_CONFIDENCE");

        // Daily reset: the original version only recorded the current block
        // number and never zeroed the counters, so "daily" limits were
        // actually lifetime limits. Fixed here to compare UTC-day buckets and
        // reset dailyTrades/dailyVolume/dailyLoss when the bucket changes.
        uint256 today = block.timestamp / 1 days;
        if (today > dailyBucket[msg.sender]) {
            dailyBucket[msg.sender] = today;
            dailyTrades[msg.sender] = 0;
            dailyVolume[msg.sender] = 0;
            dailyLoss[msg.sender] = 0;
        }

        require(dailyTrades[msg.sender] < 100, "MAX_TRADES_EXCEEDED");
        require(dailyLoss[msg.sender] < p.maxDailyLossUsd, "DAILY_LOSS_LIMIT_EXCEEDED");

        decisionLogged[decisionId] = true;
        lastDecisionId[msg.sender] = decisionId;
        dailyTrades[msg.sender]++;
        dailyVolume[msg.sender] += input.sizeUsd;

        decisions.push(TradeDecision({
            decisionId: decisionId,
            agent: msg.sender,
            asset: input.asset,
            signal: input.signal,
            strategy: input.strategy,
            confidence: input.confidence,
            entryPrice: input.entryPrice,
            sizeUsd: input.sizeUsd,
            timestamp: block.timestamp,
            riskHash: input.riskHash,
            signature: input.signature,
            executed: false,
            isShort: input.isShort
        }));

        emit DecisionLogged(
            decisionId,
            msg.sender,
            input.asset,
            input.signal,
            input.confidence,
            input.sizeUsd,
            input.riskHash
        );

        return true;
    }

    /**
     * @notice Record post-trade execution receipt.
     */
    function recordExecution(
        bytes32 decisionId,
        uint256 fillPrice,
        uint256 fillSizeUsd,
        uint256 feeUsd,
        bool success
    ) external onlyAgent {
        require(decisionLogged[decisionId], "decision not logged");
        require(!decisionExecuted[decisionId], "execution already recorded");

        uint256 len = decisions.length;
        TradeDecision storage d = decisions[len - 1];
        require(d.decisionId == decisionId, "decision mismatch");

        d.executed = true;
        decisionExecuted[decisionId] = true;

        // Direction-aware P&L: the original version treated any fill below
        // entry price as a loss, which is only true for a long. For a short
        // (isShort = true), a price drop below entry is a gain, and a price
        // rise above entry is the loss. Get this backwards and the audit
        // trail's own risk enforcement can mislabel a winning short as a
        // loss (spuriously tightening the daily loss limit) or, worse, fail
        // to record a real loss on a short that moved against the agent.
        if (success) {
            uint256 lossUsd = 0;
            if (!d.isShort && fillPrice < d.entryPrice) {
                lossUsd = (d.entryPrice - fillPrice) * d.sizeUsd / 1e8;
            } else if (d.isShort && fillPrice > d.entryPrice) {
                lossUsd = (fillPrice - d.entryPrice) * d.sizeUsd / 1e8;
            }
            if (lossUsd > 0) {
                dailyLoss[msg.sender] += lossUsd;
            }
        }

        executions.push(ExecutionReceipt({
            decisionId: decisionId,
            fillPrice: fillPrice,
            fillSizeUsd: fillSizeUsd,
            feeUsd: feeUsd,
            timestamp: block.timestamp,
            success: success
        }));

        emit TradeExecuted(decisionId, msg.sender, fillPrice, fillSizeUsd, feeUsd, success);
    }

    /**
     * @notice Verify an EIP-191 signed message (personal_sign / eth_sign).
     */
    function _isValidSignature(
        address signer,
        bytes32 digest,
        bytes memory signature
    ) internal pure returns (bool) {
        if (signer == address(0)) return false;
        if (signature.length != 65) return false;

        bytes32 r;
        bytes32 s;
        uint8 v;

        assembly ("memory-safe") {
            r := mload(add(signature, 32))
            s := mload(add(signature, 64))
            v := byte(0, mload(add(signature, 96)))
        }

        if (v < 27) v += 27;
        if (v != 27 && v != 28) return false;

        // EIP-2: reject malleable signatures
        if (uint256(s) > 0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff) {
            return false;
        }

        bytes32 ethSignedMessageHash = keccak256(abi.encodePacked(
            "\x19Ethereum Signed Message:\n32",
            digest
        ));

        return ecrecover(ethSignedMessageHash, v, r, s) == signer;
    }

    // --- Query functions ---

    function getDecision(uint256 index) external view returns (TradeDecision memory) {
        require(index < decisions.length, "index out of range");
        return decisions[index];
    }

    function getAgentDailyStats(address agent) external view returns (
        uint256 volume,
        uint256 loss,
        uint256 trades
    ) {
        return (dailyVolume[agent], dailyLoss[agent], dailyTrades[agent]);
    }

    function getDecisionCount() external view returns (uint256) {
        return decisions.length;
    }

    function getExecutionCount() external view returns (uint256) {
        return executions.length;
    }

    function getRecentDecisions(uint256 count) external view returns (TradeDecision[] memory) {
        uint256 len = decisions.length;
        uint256 start = len > count ? len - count : 0;
        TradeDecision[] memory result = new TradeDecision[](len - start);
        for (uint256 i = 0; i < result.length; i++) {
            result[i] = decisions[start + i];
        }
        return result;
    }
}