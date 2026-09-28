### Title
`NonblockingLzAppUpgradeable::retryMessage` accepts `msg.value` but never refunds or forwards it — native gas deposits are permanently locked in `ProxyOFT` - (File: contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol)

### Summary
The Metronome `ProxyOFT` contract inherits the LayerZero non-blocking receive flow, where a failed inbound message is stored and later retried via the permissionless `retryMessage` function. `retryMessage` is `payable` but completely ignores `msg.value`: it neither forwards the native tokens to the destination nor refunds them to the caller. Since `ProxyOFT` has no native-token withdrawal mechanism (no `receive`/`fallback` acceptance path that is refundable and no sweep for ETH), any native tokens sent along with a retry are permanently locked in the contract. This is the same bug class as the Maia `ArbitrumBranchBridgeAgent::_performFallbackCall` finding: a retry/fallback path that swallows the user's native gas deposit instead of refunding it.

### Finding Description
When a cross-chain `sendFrom` message fails on the destination `ProxyOFT`, `_blockingLzReceive` stores the payload hash in `failedMessages` via `_storeFailedMessage` [1](#0-0) . Anyone can then call `retryMessage` to re-execute the payload.

`retryMessage` is declared `payable` but never uses `msg.value`:

```solidity
function retryMessage(uint16 _srcChainId, bytes calldata _srcAddress, uint64 _nonce, bytes calldata _payload) public payable virtual {
    bytes32 payloadHash = failedMessages[_srcChainId][_srcAddress][_nonce];
    require(payloadHash != bytes32(0), "NonblockingLzApp: no stored message");
    require(keccak256(_payload) == payloadHash, "NonblockingLzApp: invalid payload");
    failedMessages[_srcChainId][_srcAddress][_nonce] = bytes32(0);
    _nonblockingLzReceive(_srcChainId, _srcAddress, _nonce, _payload);
    emit RetryMessageSuccess(_srcChainId, _srcAddress, _nonce, payloadHash);
}
``` [2](#0-1) 

For `ProxyOFT`, `_nonblockingLzReceive` only supports `PT_SEND`, which calls `_sendAck` → `_creditTo` → `syntheticToken.mint(to, amount)` [3](#0-2) [4](#0-3) . No native value is needed or consumed, yet any ETH attached is retained by the contract.

Unlike `sendFrom`/`_send`, where `msg.value` is forwarded to the LZ endpoint as the messaging fee with `_refundAddress` receiving the excess [5](#0-4) [6](#0-5) , the retry path has no refundee at all. `ProxyOFT` defines no `receive`/`fallback` and no function to recover native balance, so the ETH is irrecoverable.

### Impact Explanation
Permanent loss/freezing of user funds. A user (or an automated retry/keeper front-end) attaching native tokens to `retryMessage` — e.g., assuming it is needed to pay for destination gas, as in other LZ retry patterns — loses the entire `msg.value` to the `ProxyOFT` contract with no recovery path. The bridged tokens are minted correctly, but the gas deposit is silently confiscated. This matches the "excess native gas deposit not refunded" impact of the reference finding.

### Likelihood Explanation
Low-to-medium. It requires a caller to attach value to `retryMessage`. However, the function being `payable` actively invites this: LZ users are accustomed to paying gas on retry calls (e.g., `retrySettlement` in the reference report takes `msg.value` for exactly this reason), and front-ends/relayers estimating a "retry fee" would attach value. No privileged role is needed; any EOA triggers the fund lock simply by calling `retryMessage{value: X}(...)`. No modifier, cap, pause flag, or guard intercepts it — `ProxyOFT` does not override `retryMessage`, and bridging pause only affects `_debitFrom` [7](#0-6) .

### Recommendation
Either remove `payable` from `retryMessage` (reject `msg.value > 0`), or refund the caller at the end of the function:

```solidity
function retryMessage(uint16 _srcChainId, bytes calldata _srcAddress, uint64 _nonce, bytes calldata _payload) public payable virtual {
    ...
    _nonblockingLzReceive(_srcChainId, _srcAddress, _nonce, _payload);
    if (msg.value > 0) {
        (bool ok, ) = msg.sender.call{value: msg.value}("");
        require(ok, "refund failed");
    }
    emit RetryMessageSuccess(_srcChainId, _srcAddress, _nonce, payloadHash);
}
```

Alternatively, add a governor-controlled native-token sweep to `ProxyOFT` so trapped ETH is recoverable.

### Proof of Concept
Foundry fork-style PoC outline:

1. Deploy `ProxyOFT` behind a proxy with a mock/real LZ endpoint and a `SyntheticToken` + `PoolRegistry`; configure a trusted remote for `srcChain`.
2. Deliver an inbound `PT_SEND` payload that reverts inside `_sendAck` (e.g., mint paused), so `_blockingLzReceive` stores `failedMessages[src][srcAddr][nonce]`.
3. Unpause, then call `proxyOFT.retryMessage{value: 1 ether}(src, srcAddr, nonce, payload)` from an arbitrary EOA.
4. Assert: `RetryMessageSuccess` emitted, tokens minted to `to`, `address(proxyOFT).balance == 1 ether`, caller balance decreased by `1 ether`, and no function exists to recover the ETH (no `receive`-triggered sweep; `renounceOwnership`/`transferOwnership` disabled and `TokenHolder`-style sweep absent on `ProxyOFT`).

The ETH remains locked in `ProxyOFT` permanently, demonstrating the unrefunded native gas deposit.

### Citations

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L29-34)
```text
    function _blockingLzReceive(uint16 _srcChainId, bytes memory _srcAddress, uint64 _nonce, bytes memory _payload) internal virtual override {
        (bool success, bytes memory reason) = address(this).excessivelySafeCall(gasleft(), 150, abi.encodeWithSelector(this.nonblockingLzReceive.selector, _srcChainId, _srcAddress, _nonce, _payload));
        // try-catch all errors/exceptions
        if (!success) {
            _storeFailedMessage(_srcChainId, _srcAddress, _nonce, _payload, reason);
        }
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L51-61)
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
    }
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/token/oft/OFTCoreUpgradeable.sol (L97-98)
```text
        bytes memory lzPayload = abi.encode(PT_SEND, _toAddress, amount);
        _lzSend(_dstChainId, lzPayload, _refundAddress, _zroPaymentAddress, _adapterParams, msg.value);
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/token/oft/OFTCoreUpgradeable.sol (L103-110)
```text
    function _sendAck(uint16 _srcChainId, bytes memory, uint64, bytes memory _payload) internal virtual {
        (, bytes memory toAddressBytes, uint amount) = abi.decode(_payload, (uint16, bytes, uint));

        address to = toAddressBytes.toAddress(0);

        amount = _creditTo(_srcChainId, to, amount);
        emit ReceiveFromChain(_srcChainId, to, amount);
    }
```

**File:** contracts/ProxyOFT.sol (L76-82)
```text
        IPoolRegistry _poolRegistry = syntheticToken.poolRegistry();
        if (_msgSender() != from_) revert SenderIsNotTheOwner();
        if (!_poolRegistry.isBridgingActive()) revert BridgingIsPaused();
        if (!_poolRegistry.isDestinationChainSupported(dstChainId_)) revert DestinationChainNotAllowed();

        syntheticToken.burn(from_, amount_);
        return amount_;
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
