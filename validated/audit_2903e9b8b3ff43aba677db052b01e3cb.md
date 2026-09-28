### Title
Cross-chain transfers can be trapped by the destination bridged-in supply cap - (File: contracts/ProxyOFT.sol)

### Summary

`ProxyOFT.sendFrom()` burns the sender’s synthetic tokens before dispatching the LayerZero packet, but does not verify that the destination chain will be able to mint the amount. [1](#0-0)  On the destination, `_creditTo()` calls `SyntheticToken.mint()`, which reverts if the new net bridged-in supply exceeds `maxBridgedInSupply`. [2](#0-1) [3](#0-2) 

Because `NonblockingLzAppUpgradeable` catches that revert and stores the packet in `failedMessages`, the source-side burn remains final while the destination mint is deferred until sufficient bridged-in capacity becomes available. [4](#0-3) 

### Finding Description

The vulnerability is a cross-chain state race:

1. The victim calls `ProxyOFT.sendFrom(victim, dstChainId, victim, amount)` on the source chain.
2. `_debitFrom()` checks only source-side sender authorization, global bridging status, and destination-chain support. [5](#0-4) 
3. `SyntheticToken._burn()` immediately decrements the victim’s balance and total supply after checking only the source-side `maxBridgedOutSupply`. [6](#0-5) 
4. Before LayerZero delivers the victim’s packet, another unprivileged inbound bridge transfer consumes the remaining `maxBridgedInSupply` capacity on the destination.
5. Destination `_sendAck()` invokes `_creditTo()`. [7](#0-6) 
6. `SyntheticToken._mint()` increments `totalBridgedIn` and reverts with `SurpassMaxBridgingSupply` when `bridgedInSupply() > maxBridgedInSupply`. [3](#0-2) 
7. The receive wrapper stores the packet hash as failed instead of minting. [8](#0-7) 
8. `retryMessage()` can only complete the transfer after destination bridged-in capacity is freed; otherwise it reverts again. [9](#0-8) 

The relevant capacity is mutable and unprivileged: `bridgedInSupply()` is derived from cumulative inbound and outbound bridge amounts rather than an immutable packet-time value. [10](#0-9) 

### Impact Explanation

The source chain has already burned the victim’s tokens, but the destination chain has not minted them. This breaks bridge conservation temporarily and freezes the victim’s bridged funds in a failed-message state.

If `bridgedInSupply` remains at or above the cap, retries continue to revert and the funds remain uncredited indefinitely. Capacity can later be freed by outbound bridging or a cap increase, but neither is guaranteed by the victim’s transaction, so the direct impact is at least temporary freezing of user funds and potentially permanent freezing under persistent saturation.

### Likelihood Explanation

No privileged attacker action is required to consume destination capacity. Any holder of the synthetic token can call the public `sendFrom()` path, subject to having a sufficient balance and paying the LayerZero fee. [1](#0-0) 

An attacker or ordinary bridge user who observes a pending packet can consume the remaining destination-side `maxBridgedInSupply` headroom before that packet executes. LayerZero delivery latency makes the source debit and destination credit non-atomic, creating the required race window.

The existing controls do not prevent this condition: source-side `_debitFrom()` does not reserve destination capacity, and destination-side `_creditTo()` necessarily applies the cap at delivery time. [5](#0-4) [2](#0-1) 

### Recommendation

Do not make successful source-side burning depend on unconstrained mutable destination capacity. Suitable mitigations include:

- Track and reserve destination-side bridge capacity through a coordinated accounting mechanism before allowing `sendFrom()`.
- Add a refund/failure packet that restores burned tokens on the source when destination credit cannot occur.
- Route over-cap deliveries into a withdrawable escrow instead of relying on mutable retry conditions.
- At minimum, expose destination headroom in the bridge interface, although this alone does not eliminate the race.

A durable fix should preserve bridge conservation even when `SyntheticToken._mint()` rejects the delivery.

### Proof of Concept

A Foundry-style fork or unit test can reproduce the condition by emulating normal LayerZero endpoint delivery:

```solidity
function test_bridgePacketStuckWhenDestinationCapIsConsumed() public {
    uint16 srcChain = 110;
    uint64 fillerNonce = 1;
    uint64 victimNonce = 2;
    uint256 amount = 1 ether;

    // Deploy/attach destination SyntheticToken and ProxyOFT.
    // Configure trusted remote and set destination maxBridgedInSupply to 100 ether.

    // Normal delivery of another public transfer consumes all destination capacity.
    bytes memory fillerPayload = abi.encode(
        uint16(0),
        abi.encodePacked(attacker),
        uint256(100 ether)
    );

    vm.prank(address(lzEndpoint));
    dstProxyOFT.lzReceive(srcChain, trustedPath, fillerNonce, fillerPayload);

    assertEq(dstSynthetic.bridgedInSupply(), 100 ether);

    // Victim sends on the source chain. This succeeds and burns the source tokens.
    uint256 victimSourceBefore = srcSynthetic.balanceOf(victim);
    vm.prank(victim);
    srcProxyOFT.sendFrom(victim, dstChainId, victim, amount);
    assertEq(srcSynthetic.balanceOf(victim), victimSourceBefore - amount);

    // The already-sent packet now fails on destination minting.
    bytes memory victimPayload = abi.encode(
        uint16(0),
        abi.encodePacked(victim),
        amount
    );

    vm.prank(address(lzEndpoint));
    dstProxyOFT.lzReceive(srcChain, trustedPath, victimNonce, victimPayload);

    assertEq(dstSynthetic.balanceOf(victim), 0);
    assertNotEq(
        dstProxyOFT.failedMessages(srcChain, trustedPath, victimNonce),
        bytes32(0)
    );

    // Retry cannot succeed while the destination cap remains saturated.
    vm.expectRevert(SurpassMaxBridgingSupply.selector);
    dstProxyOFT.retryMessage(srcChain, trustedPath, victimNonce, victimPayload);
}
```

The endpoint `prank` is only test harness delivery of a valid packet; the state transition that fills the cap and the victim’s public `sendFrom()` call do not require privileged attacker access. The assertion demonstrates the broken invariant: source balance is burned, destination balance is not credited, and the packet remains stored as failed while the cap is saturated.

### Citations

**File:** contracts/ProxyOFT.sol (L70-82)
```text
    function _debitFrom(
        address from_,
        uint16 dstChainId_,
        bytes memory /*toAddress_*/,
        uint amount_
    ) internal override returns (uint256 _sent) {
        IPoolRegistry _poolRegistry = syntheticToken.poolRegistry();
        if (_msgSender() != from_) revert SenderIsNotTheOwner();
        if (!_poolRegistry.isBridgingActive()) revert BridgingIsPaused();
        if (!_poolRegistry.isDestinationChainSupported(dstChainId_)) revert DestinationChainNotAllowed();

        syntheticToken.burn(from_, amount_);
        return amount_;
```

**File:** contracts/ProxyOFT.sol (L86-92)
```text
    function _creditTo(
        uint16 /*srcChainId_*/,
        address toAddress_,
        uint amount_
    ) internal override returns (uint256 _received) {
        syntheticToken.mint(toAddress_, amount_);
        return amount_;
```

**File:** contracts/ProxyOFT.sol (L115-127)
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
```

**File:** contracts/SyntheticToken.sol (L157-167)
```text
     * @notice Get net bridged-in circulating supply
     * @dev The supply is calculated using `MAX(totalBridgedIn - totalBridgedOut, 0)`
     */
    function bridgedInSupply() public view returns (uint256 _supply) {
        uint256 _totalBridgedIn = totalBridgedIn;
        uint256 _totalBridgedOut = totalBridgedOut;

        if (_totalBridgedIn > _totalBridgedOut) {
            return _totalBridgedIn - _totalBridgedOut;
        }
    }
```

**File:** contracts/SyntheticToken.sol (L270-290)
```text
    function _burn(address account_, uint256 amount_) private {
        if (account_ == address(0)) revert BurnFromTheZeroAddress();

        address _msgSender = _msgSender();

        if (_isMsgSenderProxyOFT(_msgSender)) {
            totalBridgedOut += amount_;
            if (bridgedOutSupply() > maxBridgedOutSupply) revert SurpassMaxBridgingSupply();
        } else if (_isMsgSenderAmo(_msgSender)) {
            // AMO can only burn from self address.
            // account_ should be AMO and in this case it is same as _msgSender()
            if (account_ != _msgSender) revert AmoInvalidAccount();

            amoSupply -= amount_;
        }

        uint256 _currentBalance = balanceOf[account_];
        if (_currentBalance < amount_) revert BurnAmountExceedsBalance();
        unchecked {
            balanceOf[account_] = _currentBalance - amount_;
            totalSupply -= amount_;
```

**File:** contracts/SyntheticToken.sol (L333-348)
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
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L29-40)
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
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L51-60)
```text
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
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/token/oft/OFTCoreUpgradeable.sol (L103-109)
```text
    function _sendAck(uint16 _srcChainId, bytes memory, uint64, bytes memory _payload) internal virtual {
        (, bytes memory toAddressBytes, uint amount) = abi.decode(_payload, (uint16, bytes, uint));

        address to = toAddressBytes.toAddress(0);

        amount = _creditTo(_srcChainId, to, amount);
        emit ReceiveFromChain(_srcChainId, to, amount);
```
