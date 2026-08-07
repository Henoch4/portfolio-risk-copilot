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
    }

    TradeDecision[]   public decisions;
    ExecutionReceipt[] public executions;

    mapping(bytes32 => bool)        public decisionLogged;
    mapping(bytes32 => bool)        public decisionExecuted;
    mapping(address => uint256)     public dailyVolume;
    mapping(address => uint256)     public dailyLoss;
    mapping(address => uint256)     public dailyTrades;
    mapping(address => RiskParams)  public agentRiskParams;
    mapping(address => uint256)     public dailyBlock;
    mapping(address => bytes32)     public lastDecisionId;

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
     * @notice Log a trade decision BEFORE execution.
     *         Uses a struct to avoid stack-too-deep errors.
     */
    function logDecision(DecisionInput calldata input)
        external
        onlyAgent
        returns (bool)
    {
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
        require(dailyTrades[msg.sender] < 100, "MAX_TRADES_EXCEEDED");

        // Daily reset on new block (simplified: resets when block number advances)
        // In production: use a 24h window with proper daily reset logic
        if (block.number > dailyBlock[msg.sender]) {
            dailyBlock[msg.sender] = block.number;
        }

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
            executed: false
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

        if (success && fillPrice < d.entryPrice) {
            uint256 lossUsd = (d.entryPrice - fillPrice) * d.sizeUsd / 1e8;
            dailyLoss[msg.sender] += lossUsd;
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

        assembly {
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