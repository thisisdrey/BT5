### Title
Bridged synthetic tokens are burned on the source chain even when the destination `SyntheticToken.mint` cannot succeed, permanently freezing user funds — ([File: contracts/ProxyOFT.sol](contracts/ProxyOFT.sol))

### Summary
`ProxyOFT._debitFrom()` burns the user's synthetic tokens on the source chain after only two local checks (`isBridgingActive`, `isDestinationChainSupported`). On the destination chain, `_creditTo()` calls `SyntheticToken.mint()`, which can revert via `onlyIfSyntheticTokenIsActive`, `SurpassMaxBridgingSupply` (`bridgedInSupply() > maxBridgedInSupply`), or `SurpassMaxSynthSupply` (`totalSupply > maxTotalSupply`). The origin-side debit never accounts for the destination's ability to mint, so a message that cannot be credited still burns the funds. `NonblockingLzAppUpgradeable.retryMessage()` only re-executes the identical mint — it provides no refund path back to the source chain — so burned tokens stay burned while the destination condition persists. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
This mirrors the SKALE IMA bug class: the origin leg debits/locks user funds unconditionally while the destination leg applies an additional eligibility check that the sender cannot observe at send time.

- `sendFrom()` → `_send()` → `_debitFrom()` burns `amount_` from the user after checking `isBridgingActive()` and `isDestinationChainSupported(dstChainId_)` on the *source* `PoolRegistry` only. [4](#0-3) 
- On the destination, `lzReceive` → `_blockingLzReceive` → `nonblockingLzReceive` → `_nonblockingLzReceive` → `_sendAck` → `_creditTo` → `syntheticToken.mint(to, amount)`. [5](#0-4) 
- `_mint` reverts when the synthetic token is inactive (`onlyIfSyntheticTokenIsActive`), when `bridgedInSupply() > maxBridgedInSupply`, or when `totalSupply > maxTotalSupply`. [6](#0-5) 
- On failure the message is stored in `failedMessages`; `retryMessage` clears and re-executes the same payload inside one transaction, so it can only succeed if the destination-side condition later changes. There is no mechanism to route the amount back to the source chain — the source-side burn (`_burn` incrementing `totalBridgedOut`) is irreversible. [7](#0-6) [8](#0-7) 

Crucially, the blocking condition is reachable by unprivileged actors, not only by admin action:

- An attacker can legitimately mint synthetic tokens (deposit collateral → borrow) or bridge-and-hold on a chain until `totalSupply`/`bridgedInSupply` approaches `maxTotalSupply`/`maxBridgedInSupply`. Every inbound bridge credit then reverts; each victim's tokens are already burned on their source chain.
- An attacker can maintain the frozen state indefinitely by simply holding the borrowed/bridged position (no repay), since no other actor can reduce `totalSupply` or `totalBridgedIn` on that chain.

### Impact Explanation
Bridge conservation is broken: tokens are destroyed on the source chain while the corresponding mint on the destination can fail for conditions the source could not verify. Affected users' funds are frozen for as long as the destination mint reverts — permanently if `totalSupply`/`bridgedInSupply` never falls below the caps or the token is never reactivated — with no self-service recovery because `retryMessage` reverts on the same condition. [3](#0-2) 

### Likelihood Explanation
Medium. It requires destination supply to be near a cap or the token halted — states that can arise from ordinary protocol usage and can be deliberately induced by an unprivileged attacker holding a minted/bridged position. Once in that state, every subsequent `sendFrom` on every source chain targeting it produces frozen funds automatically.

### Recommendation
- Add a symmetric "can credit" signal: e.g., expose remaining `maxBridgedInSupply`/`maxTotalSupply` headroom and have frontends/keepers block sends, or better, implement a source-side refund path — on destination failure, send a LayerZero message back that re-mints the burned amount to the sender on the origin chain.
- Alternatively, make `retryMessage`-style recovery always resolvable by allowing a permissionless `refundToSource(nonce)` that credits the burned amount back on the origin chain rather than only retrying the failing mint.
- Consider a timelock/cooldown on lowering `maxBridgedInSupply`/`maxTotalSupply` or halting the token while in-flight messages may exist, analogous to the audit recommendation of a timelock on disabling `automaticDeploy`.

### Proof of Concept
Hardhat/Foundry two-fork or mock-endpoint test (same pattern as existing `ProxyOFT` tests):

```solidity
// Setup: SyntheticToken msUSD + ProxyOFT on chainA and chainB mocks,
// lzEndpoint mocked so sendFrom on A triggers lzReceive on B.

// 1) Governor sets maxBridgedInSupply on chainB msUSD = 100e18 (or attacker
//    borrows on chainB until totalSupply == maxTotalSupply).

// 2) Attacker (or prior flow) bridges 100e18 to chainB so bridgedInSupply == cap.
proxyOFT_A.sendFrom(attacker, chainB_Id, attacker, 100e18);
deliverToB(payload);            // mints 100e18 to attacker on B

// 3) Victim bridges 50e18 A -> B. Burn succeeds on A.
uint256 victimBalBefore = msUSD_A.balanceOf(victim);
proxyOFT_A.sendFrom(victim, chainB_Id, victim, 50e18);
assertEq(msUSD_A.balanceOf(victim), victimBalBefore - 50e18); // burned

// 4) Delivery on B: _creditTo -> msUSD_B.mint reverts SurpassMaxBridgingSupply;
//    NonblockingLzApp stores failedMessages[chainA][src][nonce].
deliverToB(victimPayload);      // emits MessageFailed, no mint

// 5) Anyone retries; the identical mint still reverts.
vm.expectRevert(SurpassMaxBridgingSupply.selector);
proxyOFT_B.retryMessage(chainA_Id, srcBytes, nonce, victimPayload);

// 6) No recovery path exists: victim's 50e18 is burned on A and unmintable on B
//    unless governor raises the cap (or supply organically decreases).
```

Key assertions: `msUSD_A.balanceOf(victim)` decreased by `amount` (burn is final in `_burn`), `failedMessages` holds the payload hash, and `retryMessage` re-executes `_nonblockingLzReceive` → `mint` → revert, confirming funds frozen while `bridgedInSupply() > maxBridgedInSupply`.

### Citations

**File:** contracts/ProxyOFT.sol (L70-93)
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

**File:** contracts/SyntheticToken.sol (L270-294)
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

        emit Transfer(account_, address(0), amount_);
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

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/lzApp/NonblockingLzAppUpgradeable.sol (L37-46)
```text
    function _storeFailedMessage(uint16 _srcChainId, bytes memory _srcAddress, uint64 _nonce, bytes memory _payload, bytes memory _reason) internal virtual {
        failedMessages[_srcChainId][_srcAddress][_nonce] = keccak256(_payload);
        emit MessageFailed(_srcChainId, _srcAddress, _nonce, _payload, _reason);
    }

    function nonblockingLzReceive(uint16 _srcChainId, bytes calldata _srcAddress, uint64 _nonce, bytes calldata _payload) public virtual {
        // only internal transaction
        require(_msgSender() == address(this), "NonblockingLzApp: caller must be LzApp");
        _nonblockingLzReceive(_srcChainId, _srcAddress, _nonce, _payload);
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

**File:** contracts/dependencies/@layerzerolabs/solidity-examples/contracts-upgradeable/token/oft/OFTCoreUpgradeable.sol (L103-110)
```text
    function _sendAck(uint16 _srcChainId, bytes memory, uint64, bytes memory _payload) internal virtual {
        (, bytes memory toAddressBytes, uint amount) = abi.decode(_payload, (uint16, bytes, uint));

        address to = toAddressBytes.toAddress(0);

        amount = _creditTo(_srcChainId, to, amount);
        emit ReceiveFromChain(_srcChainId, to, amount);
    }
```
