### Title
Attacker can saturate `maxBridgedInSupply` / `maxTotalSupply` so inbound LayerZero mints revert and victim bridge transfers are stuck as failed messages - (File: contracts/SyntheticToken.sol)

### Summary
`ProxyOFT._creditTo` mints via `SyntheticToken.mint`, which reverts with `SurpassMaxBridgingSupply` or `SurpassMaxSynthSupply` once the bridged-in or total-supply caps are hit. Because the LZ app is non-blocking, a reverted receive is only stored in `failedMessages` and the tokens remain burned on the source chain. An unprivileged attacker can deliberately push the bridged-in counter to the cap by bridging their own synth, causing every subsequent inbound transfer from other users to fail and be queued as a failed message — freezing their funds until the governor raises the cap. This mirrors the bug class: an attacker-forced state transition makes subsequent inbound messages unprocessable.

### Finding Description
`SyntheticToken._mint` increments `totalBridgedIn` when the caller is `proxyOFT`, then enforces `bridgedInSupply() > maxBridgedInSupply` and `totalSupply > maxTotalSupply` reverts [1](#0-0) . On the receive path, `ProxyOFT._creditTo` calls `syntheticToken.mint(toAddress_, amount_)` [2](#0-1) , which runs inside `NonblockingLzAppUpgradeable._blockingLzReceive`; on revert the payload hash is stored in `failedMessages` instead of crediting the recipient [3](#0-2) . The burn side (`_debitFrom`) has already destroyed the user's tokens on the source chain [4](#0-3) , so a failed mint leaves the user with no tokens on either chain until `retryMessage` succeeds — which is impossible while the cap is saturated.

### Impact Explanation
Temporary freezing of user funds: all inbound bridge transfers of that synthetic token fail and sit in `failedMessages` while the cap is saturated. Affected users cannot recover on the destination chain (`retryMessage` re-executes the same reverting `mint`) nor on the source chain (tokens already burned). Recovery requires the governor to raise `maxBridgedInSupply`/`maxTotalSupply` or net bridging flows to reverse. Note `bridgedInSupply()` is `max(totalBridgedIn - totalBridgedOut, 0)`, so it only decreases when someone bridges *out* — a normal user whose tokens are stuck cannot help unwind it.

### Likelihood Explanation
Fully reachable by an unprivileged EOA. The attacker acquires synth (e.g. via `Pool.swap` or issuing debt), then calls `ProxyOFT.sendFrom` repeatedly to bridge out-and-back or accumulate `totalBridgedIn` on the target chain until `bridgedInSupply()` reaches `maxBridgedInSupply`. Only the attacker's own capital is temporarily locked; their failed/succeeded transfers can later be retried or bridged back after the cap is lifted. `isBridgingActive` and `isDestinationChainSupported` checks do not prevent this, and no reentrancy guard or pause flag stops repeated `sendFrom` calls. The cost is bounded by the remaining cap headroom; if governance configured a tight cap relative to circulating supply, the attack is cheap.

### Recommendation
- Make the credit path non-reverting for cap checks: credit to the recipient up to the cap and route the excess to a claimable balance, or escrow `to → amount` and let users pull after capacity frees.
- Alternatively, decrement/return a "credit reservation" on the source chain so cap state cannot be manipulated independently of in-flight messages.
- At minimum, emit monitoring on `MessageFailed` and ensure `maxBridgedInSupply` has enough headroom that filling it is economically prohibitive.

### Proof of Concept
Foundry fork sketch (two chains simulated, or direct call of the receive path on a fork of the deployed config):

```solidity
// fork destination chain (e.g. Base) where MsUSDProxyOFT is deployed
function testCapSaturatingFreezesInbound() public {
    ISyntheticToken synth = proxyOFT.syntheticToken();
    IPoolRegistry reg = synth.poolRegistry();

    uint256 cap = synth.maxBridgedInSupply();
    uint256 headroom = cap - synth.bridgedInSupply();

    // attacker holds synth on this chain and bridges it out to another chain
    // (increments totalBridgedOut) then back (increments totalBridgedIn) OR
    // simpler: acquire synth on source chain and send `headroom` in.
    // After fills, simulate an inbound victim message via the stored path:
    // call _blockingLzReceive path by impersonating the lz endpoint with a
    // PT_SEND payload of amount A for `victim`.
    vm.prank(address(endpoint));
    proxyOFT.lzReceive(srcChainId, srcAddr, nonce, payloadForVictim);
    // expect: nonblockingLzReceive reverts SurpassMaxBridgingSupply,
    // message stored in failedMessages[srcChainId][srcAddr][nonce]

    assertTrue(proxyOFT.failedMessages(srcChainId, srcAddr, nonce) != bytes32(0));
    assertEq(synth.balanceOf(victim), 0); // burned on src, not minted on dst

    // retry reverts while cap still saturated
    vm.expectRevert();
    proxyOFT.retryMessage(srcChainId, srcAddr, nonce, payloadForVictim);
}
```

The same test applies to `maxTotalSupply`: an attacker mints synth via `SmartFarmingManager.leverage`/pool issuance to push `totalSupply` to `maxTotalSupply`, after which every `_creditTo` reverts with `SurpassMaxSynthSupply`.

### Citations

**File:** contracts/SyntheticToken.sol (L338-348)
```text
        if (_isMsgSenderProxyOFT(_msgSender)) {
            totalBridgedIn += amount_;
            if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply();
        } else if (_isMsgSenderAmo(_msgSender)) {
            amoSupply += amount_;
            if (amoSupply > maxAmoSupply) revert SurpassMaxAmoSupply();
        }

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxSynthSupply();
        balanceOf[account_] += amount_;
```

**File:** contracts/ProxyOFT.sol (L75-83)
```text
    ) internal override returns (uint256 _sent) {
        IPoolRegistry _poolRegistry = syntheticToken.poolRegistry();
        if (_msgSender() != from_) revert SenderIsNotTheOwner();
        if (!_poolRegistry.isBridgingActive()) revert BridgingIsPaused();
        if (!_poolRegistry.isDestinationChainSupported(dstChainId_)) revert DestinationChainNotAllowed();

        syntheticToken.burn(from_, amount_);
        return amount_;
    }
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

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L29-39)
```text
    function _blockingLzReceive(uint16 _srcChainId, bytes memory _srcAddress, uint64 _nonce, bytes memory _payload) internal virtual override {
        (bool success, bytes memory reason) = address(this).excessivelySafeCall(gasleft(), 150, abi.encodeWithSelector(this.nonblockingLzReceive.selector, _srcChainId, _srcAddress, _nonce, _payload));
        // try-catch all errors/exceptions
        if (!success) {
            _storeFailedMessage(_srcChainId, _srcAddress, _nonce, _payload, reason);
        }
    }

    function _storeFailedMessage(uint16 _srcChainId, bytes memory _srcAddress, uint64 _nonce, bytes memory _payload, bytes memory _reason) internal virtual {
        failedMessages[_srcChainId][_srcAddress][_nonce] = keccak256(_payload);
        emit MessageFailed(_srcChainId, _srcAddress, _nonce, _payload, _reason);
```
