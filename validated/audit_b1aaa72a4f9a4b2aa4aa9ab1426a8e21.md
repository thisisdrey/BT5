### Title
Attacker can freeze inbound bridged msAsset mints indefinitely by saturating `maxBridgedInSupply` (File: contracts/SyntheticToken.sol)

### Summary
Cross-chain settlement of synthetic assets is non-final: `_creditTo` in `ProxyOFT` mints on the destination chain, but `_mint` reverts with `SurpassMaxBridgingSupply` when the net bridged-in supply exceeds `maxBridgedInSupply`. The failed LayerZero message is parked in `failedMessages`, and delivery only completes when someone retries successfully. An unprivileged attacker can deliberately saturate the bridged-in cap so that a victim's inbound bridge cannot be credited for an arbitrary, attacker-controlled duration — the on-chain equivalent of a payment with an undetermined confirmation time that can be "rolled back" (held in limbo) long after the sender's tokens were burned.

### Finding Description
`ProxyOFT._creditTo` calls `syntheticToken.mint(toAddress_, amount_)` during `lzReceive` (contracts/ProxyOFT.sol:86-93). Inside `SyntheticToken._mint`, when the caller is the ProxyOFT, `totalBridgedIn` is incremented and the tx reverts if `bridgedInSupply() > maxBridgedInSupply` (contracts/SyntheticToken.sol:338-340). `bridgedInSupply()` is `totalBridgedIn - totalBridgedOut` (contracts/SyntheticToken.sol:160-167).

Because the receive path is wrapped by `NonblockingLzAppUpgradeable._blockingLzReceive` in a try/catch, this revert does not fail the LZ delivery permanently — it stores `failedMessages[srcChainId][srcAddress][nonce] = keccak256(payload)` (contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol:29-40). The tokens are already burned on the source chain (`_debitFrom`, ProxyOFT.sol:81), so the value is in neither chain until `retryMessage` succeeds — and `retryMessage` reverts while the cap remains saturated (lines 51-61).

Attack flow:

1. Attacker legitimately acquires msUSD/msETH on chain A and calls `ProxyOFT.sendFrom` (public, payable; only requires `isBridgingActive` and a supported destination, ProxyOFT.sol:70-83) repeatedly until `bridgedInSupply` on chain B equals `maxBridgedInSupply`.
2. Victim calls `sendFrom` on chain A; tokens are burned there.
3. On chain B, `lzReceive → _creditTo → _mint` reverts with `SurpassMaxBridgingSupply`; the payload is stored as failed. Victim receives nothing.
4. The mint cannot complete until either the governor raises the cap, or someone bridges out (`sendFrom` on B increases `totalBridgedOut`, lowering `bridgedInSupply`) — both outside the victim's control. The attacker can even keep the cap saturated by re-bridging in whenever room appears, and can permissionlessly trigger `retryMessage`/`retryOFTReceived` at a time of their choosing.

The attacker pays only bridge fees and temporarily parks their own capital; they can unwind by bridging out at will, so the cost is low.

### Impact Explanation
Temporary freezing of funds for an undetermined period: the victim's synthetic tokens are burned on the source chain but cannot be minted on the destination chain while the cap is saturated. There is no deadline, no automatic refund path, and no way for the victim to force settlement — `retryMessage` is permissionless but simply reverts while `bridgedInSupply` exceeds the cap. This directly mirrors the reported bug class: the "payment" (bridged credit) has no reliable confirmation bound and its finalization is gated by conditions a third party can manipulate.

### Likelihood Explanation
Medium. `maxBridgedInSupply` is a governance-set cap and may be high in practice, but `totalBridgedIn` is cumulative (never reset), so historical inflows count against it permanently and the cap can be reached by ordinary usage plus modest attacker top-ups. The attack requires only public `sendFrom` calls and flash-loanable/ms-swap-acquired synthetic tokens; no privileged role, oracle manipulation, or malicious endpoint is needed. Note the attacker's own bridged-in tokens count toward the cap only net of outflows, so sustaining the freeze requires keeping capital parked (or repeatedly re-bridging), which bounds the griefing duration by attacker capital, not by any protocol safety mechanism.

### Recommendation
- Track bridged-in capacity against a rolling/current supply measure rather than lifetime `totalBridgedIn` (e.g., decrement `totalBridgedIn` on local burn, or compare against current supply sourced from the bridge).
- Alternatively, allow the mint to proceed but queue the excess, or add a permissionless refund path (credit back / unlock on source chain) when destination minting fails, so funds are never stuck in limbo.
- At minimum, emit clear events and document that `sendFrom` does not guarantee bounded-time settlement when `bridgedInSupply` is near `maxBridgedInSupply`, and monitor the headroom.

### Proof of Concept
```solidity
// Hardhat/Foundry fork test sketch (destination chain B)
// 1. Attacker holds msUSD on chain A (or obtains via Pool.swap/mint).
// 2. Fill the cap: repeat until bridgedInSupply() == maxBridgedInSupply
while (msUsd.bridgedInSupply() < maxBridgedIn) {
    proxyOFT_A.sendFrom{value: fee}(attacker, chainIdB, attacker, amount);
    deliverLzMessage(); // delivers PT_SEND -> mints to attacker on B
}
// 3. Victim bridges
proxyOFT_A.sendFrom{value: fee}(victim, chainIdB, victim, victimAmount);
//    -> msUSD burned on A (totalBridgedOut on A += amount)

// 4. Deliver victim's message on B
deliverLzMessage(); // _creditTo -> _mint reverts SurpassMaxBridgingSupply
//    -> caught by _blockingLzReceive, stored in failedMessages[...]
assertEq(msUsd.balanceOf(victim), 0); // victim got nothing

// 5. Anyone retries while cap saturated -> still reverts
vm.expectRevert("SurpassMaxBridgingSupply");
proxyOFT_B.retryMessage(chainIdA, srcAddr, nonce, payload);

// 6. Attacker chooses when (if ever) to free capacity by bridging out,
//    then calls retryMessage to finally settle the victim's credit.
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

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

**File:** contracts/SyntheticToken.sol (L333-350)
```text
    function _mint(address account_, uint256 amount_) private onlyIfSyntheticTokenIsActive {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        address _msgSender = _msgSender();

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
        emit Transfer(address(0), account_, amount_);
    }
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L29-61)
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
    }

    function nonblockingLzReceive(uint16 _srcChainId, bytes calldata _srcAddress, uint64 _nonce, bytes calldata _payload) public virtual {
        // only internal transaction
        require(_msgSender() == address(this), "NonblockingLzApp: caller must be LzApp");
        _nonblockingLzReceive(_srcChainId, _srcAddress, _nonce, _payload);
    }

    //@notice override this function
    function _nonblockingLzReceive(uint16 _srcChainId, bytes memory _srcAddress, uint64 _nonce, bytes memory _payload) internal virtual;

    function retryMessage(uint16 _srcChainId, bytes calldata _srcAddress, uint64 _nonce, bytes calldata _payload) public payable virtual {
        // assert there is message to retry
        bytes32 payloadHash = failedMessages[_srcChainId][_srcAddress][_nonce];
        require(payloadHash != bytes32(0), "NonblockingLzApp: no stored message");
        require(keccak256(_payload) == payloadHash, "NonblockingLzApp: invalid payload");
        // clear the stored message
        failedMessages[_srcChainId][_srcAddress][_nonce] = bytes32(0);
        // execute the message. revert if it fails again
        _nonblockingLzReceive(_srcChainId, _srcAddress, _nonce, _payload);
        emit RetryMessageSuccess(_srcChainId, _srcAddress, _nonce, payloadHash);
    }
```
