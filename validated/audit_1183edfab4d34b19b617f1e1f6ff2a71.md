### Title
Attacker can permanently stall the inbound LayerZero bridge channel by bridging an amount that makes `SyntheticToken.mint` revert with `SurpassMaxBridgingSupply` - ([File: contracts/ProxyOFT.sol](contracts/ProxyOFT.sol))

### Summary
`ProxyOFT` inherits LayerZero v1's *blocking* `LzAppUpgradeable` receive path: `lzReceive` calls `_blockingLzReceive` directly and any revert propagates to the endpoint, which stores the message as a failed payload and blocks all subsequent inbound messages for that `(srcChainId, srcAddress)` until governance calls `forceResumeReceive`. Because `ProxyOFT._creditTo` calls `SyntheticToken.mint`, which reverts with `SurpassMaxBridgingSupply` when `bridgedInSupply() > maxBridgedInSupply`, an unprivileged user can permissionlessly craft a bridge transfer (a "validly encoded but invalid per business rules" message — the same bug class as an invalid `guided_json` schema crashing vLLM's shared engine) that is guaranteed to revert on every delivery attempt, stalling the entire inbound bridge channel. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
- `ProxyOFT` extends `ComposableOFTCoreUpgradeable` → `OFTCoreUpgradeable` → `LzAppUpgradeable`. Unlike `NonblockingLzAppUpgradeable` (which catches failures in a failed-messages map and lets later nonces proceed), `LzAppUpgradeable.lzReceive` invokes `_blockingLzReceive` with no try/catch, so a revert bubbles up to the endpoint, stores the payload, and enforces ordered delivery — every later message from that source chain is queued behind it.
- On receipt of a `PT_SEND` packet, `_blockingLzReceive` → `OFTCoreUpgradeable._creditTo` → `SyntheticToken.mint(to_, amount_)` → `_mint`. Since the caller is the ProxyOFT, `_mint` executes `totalBridgedIn += amount_; if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply()`.
- Attack: on a supported source chain, the attacker obtains synth tokens (permissionless `DebtToken.issue`), then calls `ProxyOFT.sendFrom(from_, dstChainId_, to_, amount_)` on the source chain with `amount_` chosen so that on the destination chain `totalBridgedIn + amount_ - totalBridgedOut > maxBridgedInSupply` (e.g., when `bridgedInSupply` is already near the cap — a normal, reachable state). `_debitFrom` burns the attacker's tokens on source and `_lzSend` dispatches; on destination, `mint` reverts with `SurpassMaxBridgingSupply`, the endpoint stores the payload, and the inbound channel for that chain halts.
- The attacker pays only gas: they can later call `sendFrom` on the destination chain (outbound still works) to raise `totalBridgedOut`, lowering `bridgedInSupply`, then call `retryMessage` to recover their funds. [4](#0-3) [5](#0-4) 

### Impact Explanation
Temporary freezing of user funds: every honest user's in-flight bridge transfer on that source→destination path (already burned on source, not yet minted) is frozen behind the poisoned message, and all subsequent inbound bridging stops until the governor calls `forceResumeReceive` (which permanently skips the stored nonce) or `bridgedInSupply` drops. The attacker can repeatedly re-poison the channel cheaply, since each retry only needs a message that reverts on receive (cap re-hit, `SyntheticIsInactive` toggling aside). This mirrors the vLLM report: one malformed-but-deliverable request kills the shared processing pipeline rather than just the sender's transaction.

### Likelihood Explanation
Reachable by any EOA with no privileged role: `sendFrom` is public, `isDestinationChainSupported` only requires the chain to be allow-listed (it must be for the bridge to function), and `bridgedInSupply` approaching `maxBridgedInSupply` occurs naturally as bridged volume grows — and can be engineered by the attacker bridging in their own tokens first (each preceding transfer legitimately mints). No oracle manipulation, malicious endpoint, or governance action is required to trigger; only recovery requires the governor. Cost is roughly the LZ fee plus temporarily locked (recoverable) tokens.

### Recommendation
Move to non-blocking receive semantics so one failing message cannot stall the channel: inherit `NonblockingLzAppUpgradeable` (or wrap `_creditTo`/`mint` in try/catch inside `_blockingLzReceive`, emitting a failure event and storing failed credits for later permissionless retry). Alternatively, replace the `revert SurpassMaxBridgingSupply` cap enforcement on the receive path with a queued/mint-later mechanism, since a cap violation on receipt already has the tokens burned on the source chain.

### Proof of Concept
Hardhat/Foundry fork outline (deterministic, no malicious endpoint):

```solidity
// Setup: msETH exists on chain A (source) and chain B (dest), bridged via ProxyOFT.
// 1) On B, note bridgedInSupply_B = msETH_B.bridgedInSupply() and cap = msETH_B.maxBridgedInSupply().
// 2) Attacker on A: debtToken.issue(X, attacker); // mint msETH_A to self
// 3) Choose amount = cap - bridgedInSupply_B + 1 (or first bridge in legitimately to approach cap).
// 4) proxyOFT_A.sendFrom(attacker, chainIdB, attacker, amount) {value: nativeFee}
//    -> _debitFrom burns on A, lzSend emits packet.
// 5) Relayer delivers packet on B:
//    lzEndpoint.receivePayload -> proxyOFT_B.lzReceive -> _blockingLzReceive
//    -> _creditTo -> msETH_B.mint -> bridgedInSupply() > maxBridgedInSupply
//    -> revert SurpassMaxBridgingSupply -> endpoint stores payload, nonce N blocks.
// 6) Any user sendFrom for nonce > N remains undeliverable (ordered channel).
// 7) attacker calls proxyOFT_B.sendFrom(attacker, chainIdA, attacker, smallAmt)
//    to raise totalBridgedOut_B, then retryMessage(srcChainId, srcAddress, payload)
//    succeeds -> attacker recovers funds; other users stayed frozen until step 7
//    or governor forceResumeReceive().
```

Uncertainty note: the exact stored-payload/retry mechanics live in the LayerZero endpoint contract (not in this repo); the blocking-until-`forceResumeReceive` behavior is the documented LZ v1 semantic that `LzAppUpgradeable` explicitly retains ("the default behaviour of LayerZero is blocking"), which `lzReceive`'s uncaught propagation confirms on Metronome's side.

### Citations

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/LzAppUpgradeable.sol (L63-72)
```text
        _blockingLzReceive(_srcChainId, _srcAddress, _nonce, _payload);
    }

    // abstract function - the default behaviour of LayerZero is blocking. See: NonblockingLzApp if you dont need to enforce ordered messaging
    function _blockingLzReceive(
        uint16 _srcChainId,
        bytes memory _srcAddress,
        uint64 _nonce,
        bytes memory _payload
    ) internal virtual;
```

**File:** contracts/ProxyOFT.sol (L86-93)
```text
    function _creditTo(
        uint16 /*srcChainId_*/,
        address toAddress_,
        uint amount_
    ) internal override returns (uint256 _received) {
        syntheticToken.mint(toAddress_, amount_);
        return amount_;
    }
```

**File:** contracts/ProxyOFT.sol (L115-128)
```text
    function sendFrom(address from_, uint16 dstChainId_, address to_, uint256 amount_) external payable {
        _send({
            _from: from_,
            _dstChainId: dstChainId_,
            _toAddress: abi.encodePacked(to_),
            _amount: amount_,
            _refundAddress: payable(from_),
            _zroPaymentAddress: address(0),
            _adapterParams: abi.encodePacked(
                uint16(1), // LZ_ADAPTER_PARAMS_VERSION
                syntheticToken.poolRegistry().lzBaseGasLimit()
            )
        });
    }
```

**File:** contracts/SyntheticToken.sol (L160-167)
```text
    function bridgedInSupply() public view returns (uint256 _supply) {
        uint256 _totalBridgedIn = totalBridgedIn;
        uint256 _totalBridgedOut = totalBridgedOut;

        if (_totalBridgedIn > _totalBridgedOut) {
            return _totalBridgedIn - _totalBridgedOut;
        }
    }
```

**File:** contracts/SyntheticToken.sol (L338-340)
```text
        if (_isMsgSenderProxyOFT(_msgSender)) {
            totalBridgedIn += amount_;
            if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply();
```
