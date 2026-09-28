### Title
Unprivileged user can exhaust `maxBridgedInSupply` and block all inbound cross-chain mints of a synthetic token - ([File: contracts/SyntheticToken.sol](contracts/SyntheticToken.sol))

### Summary
`SyntheticToken` enforces a per-token ceiling `maxBridgedInSupply` on `bridgedInSupply() = totalBridgedIn - totalBridgedOut` inside `_mint`, which is triggered for every inbound LayerZero mint via `ProxyOFT._creditTo`. Any EOA can mint synths on a source chain (via `DebtToken.issue` against collateral) and call `ProxyOFT.sendFrom` to bridge them to the target chain, monotonically increasing `totalBridgedIn`. Once `bridgedInSupply()` reaches the cap, every subsequent legitimate inbound bridge transfer reverts with `SurpassMaxBridgingSupply` inside `_mint` [1](#0-0) . This is the Metronome analog of the reported bug class: an unprivileged actor squats a shared, bounded allocation namespace (here, the bridged-in supply budget) that a protocol flow assumes is still available, causing that flow to revert.

### Finding Description
- Inbound bridging path: remote `ProxyOFT.sendFrom` → `_debitFrom` burns on source [2](#0-1)  → LayerZero delivery → `_creditTo` → `syntheticToken.mint(toAddress_, amount_)` [3](#0-2) .
- `SyntheticToken._mint` sees `_msgSender() == proxyOFT`, increments `totalBridgedIn` by `amount_`, and reverts if `bridgedInSupply() > maxBridgedInSupply` [4](#0-3) .
- Because `bridgedInSupply` is cumulative (`totalBridgedIn - totalBridgedOut`, tracked in `SyntheticTokenStorageV1` [5](#0-4) ), one attacker bridging in `maxBridgedInSupply - bridgedInSupply()` worth of tokens consumes the entire remaining allowance for all users.
- Failed deliveries are stored by `NonblockingLzAppUpgradeable._storeFailedMessage` into `failedMessages` and can only be released via `retryMessage`, which re-executes the same reverting `mint` [6](#0-5) .
- Relevant modifiers (`onlyIfCanMint`, `BridgingIsPaused`, `DestinationChainNotAllowed`, `SenderIsNotTheOwner`) do not prevent this: the attacker uses a legitimate public `sendFrom` on a supported chain, and bridging in also *decreases* `bridgedOutSupply`, so no counter-check stops the grief.

### Impact Explanation
All subsequent inbound cross-chain transfers of that synth fail and sit in `failedMessages`. Victims' tokens are burned on the source chain but never minted on the destination — funds are frozen until the attacker voluntarily bridges back out or governance raises `maxBridgedInSupply` and someone retries the messages. This is a temporary freezing of user funds and a liveness break of the bridge-conservation invariant (burned on source, not credited on destination) caused entirely by an unprivileged actor.

### Likelihood Explanation
The attacker's only requirements are (a) holding synths on any supported remote chain — obtainable permissionlessly by depositing collateral and calling `DebtToken.issue` there — and (b) LayerZero messaging fees. The bridged tokens remain the attacker's property and can be bridged back at any time, so the economic cost is limited to gas/LZ fees plus temporary capital. The attack is repeatable: any time governance raises the cap or users bridge out, the attacker can refill it. Whether `maxBridgedInSupply` is set low enough to be cheap to exhaust depends on deployment configuration, but the mechanism itself imposes no per-account or per-message rate limiting.

### Recommendation
Apply the per-mint supply check only to the headroom actually needed rather than a single global bucket that any inbound transfer can saturate — e.g., track bridged-in supply per source chain with per-source caps, or revert only the overflow while minting the rest, or treat `SurpassMaxBridgingSupply` as a recoverable condition that does not burn the failed-message budget symmetric to `maxBridgedOutSupply`. Alternatively, charge/lock a refundable deposit on `sendFrom` proportional to consumed `bridgedInSupply` headroom so exhausting the cap costs the attacker the value they are locking for everyone else.

### Proof of Concept
Hardhat sketch on a fork/configured test deployment (mirroring `test/SyntheticToken.test.ts`):

```typescript
// setup: msUSD synthetic token, proxyOFT, governor sets maxBridgedInSupply = CAP
await msUSD.connect(governor).updateMaxBridgedInSupply(CAP)

// attacker holds `CAP` synths on source chain and calls sendFrom(srcChain -> this chain)
// simulate deliveries by impersonating lzEndpoint as in test/SyntheticToken.plasma.test.ts
const payload = ethers.utils.defaultAbiCoder.encode(
  ['uint16', 'bytes', 'uint256'],
  [0, attacker.address, CAP]
)
await proxyOFT.connect(lzEndpointSigner).lzReceive(SRC_CHAIN_ID, path, 1, payload)
// assert: bridgedInSupply() == CAP

// victim's legitimate inbound transfer
const victimPayload = ethers.utils.defaultAbiCoder.encode(
  ['uint16', 'bytes', 'uint256'],
  [0, victim.address, ethers.utils.parseEther('1')]
)
await proxyOFT.connect(lzEndpointSigner).lzReceive(SRC_CHAIN_ID, path, 2, victimPayload)
// nonblocking path stores failed message; direct mint reverts SurpassMaxBridgingSupply

// retry is permissionless but still reverts while cap is saturated
await expect(
  proxyOFT.retryMessage(SRC_CHAIN_ID, path, 2, victimPayload)
).to.be.reverted // SurpassMaxBridgingSupply inside SyntheticToken._mint

expect(await msUSD.balanceOf(victim.address)).to.eq(0) // victim funds frozen
```

Caveat: the exact deployed value of `maxBridgedInSupply` for each synth could not be verified from the index (deployment JSONs contain ABI/bytecode, not live storage), so the economic feasibility on a specific chain depends on that on-chain configuration.

### Citations

**File:** contracts/SyntheticToken.sol (L338-347)
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
```

**File:** contracts/ProxyOFT.sol (L70-83)
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

**File:** contracts/storage/SyntheticTokenStorage.sol (L62-79)
```text
     * @notice Track amount received cross-chain
     */
    uint256 public totalBridgedIn;

    /**
     * @notice Track amount sent cross-chain
     */
    uint256 public totalBridgedOut;

    /**
     * @notice Maximum allowed bridged-in (mint-related) supply
     */
    uint256 public maxBridgedInSupply;

    /**
     * @notice Maximum allowed bridged-out (burn-related) supply
     */
    uint256 public maxBridgedOutSupply;
```

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L29-59)
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
```
