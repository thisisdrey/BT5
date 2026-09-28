### Title
ProxyOFT allows bridging to `address(0)` although the destination mint always reverts, permanently burning user funds - ([File: contracts/ProxyOFT.sol])

### Summary
`OptimismPortal` rejects withdrawal targets equal to the portal at finalization but `L2ToL1MessagePasser` does not validate them at initiation. The Metronome analog is the LayerZero OFT bridge: `ProxyOFT._debitFrom` burns the sender's synthetic tokens on the source chain without validating the destination `toAddress_`, while the destination-side credit path (`SyntheticToken._mint`) unconditionally reverts for `account_ == address(0)` (`MintToTheZeroAddress`). The failed LayerZero message is stored for retry, but the payload is immutable, so every retry reverts forever — the burned tokens can never be recovered.

### Finding Description
`ProxyOFT.sendFrom` (the user-friendly wrapper) and `OFTCoreUpgradeable.sendFrom` both route into `OFTCoreUpgradeable._send`, which calls `ProxyOFT._debitFrom`. `_debitFrom` checks the sender, the bridging-active flag, and the destination-chain allowlist, then calls `syntheticToken.burn(from_, amount_)` — `toAddress_` is deliberately ignored (the parameter is commented out in the signature).

On the destination chain, `_sendAck` decodes `toAddress` and calls `ProxyOFT._creditTo`, which calls `syntheticToken.mint(toAddress_, amount_)`. `SyntheticToken._mint` reverts with `MintToTheZeroAddress` when `account_ == address(0)`. Because `NonblockingLzAppUpgradeable` stores the failed message and `retryMessage` re-executes the identical payload, the revert is permanent: tokens were burned on the source chain but can never be minted on the destination chain.

The same asymmetry exists for the destination-side `SurpassMaxBridgingSupply` / `SurpassMaxSynthSupply` / `SyntheticIsInactive` checks: initiation does not pre-validate conditions that finalization enforces, although those cases are recoverable once governance changes the state. The zero-address case is not recoverable.

Key code:
- `contracts/ProxyOFT.sol:70-83` — `_debitFrom` ignores `toAddress_` and burns.
- `contracts/ProxyOFT.sol:86-93` — `_creditTo` blindly mints to `toAddress_`.
- `contracts/ProxyOFT.sol:115-128` — `sendFrom` accepts any `to_` with no zero-address check.
- `contracts/SyntheticToken.sol:333-350` — `_mint` reverts `MintToTheZeroAddress` and enforces `maxBridgedInSupply`/`maxTotalSupply`.

### Impact Explanation
Permanent loss of user funds. A user who calls `sendFrom` (or the raw `sendFrom` with a zero/invalid `toAddress` byte string) has their msAsset burned on the source chain while the destination mint reverts forever; the stored failed message can never succeed on retry. Bridge conservation is violated: `totalBridgedOut` increased on the source with no corresponding `totalBridgedIn` on the destination.

### Likelihood Explanation
Low-to-medium. It requires a user mistake (passing `address(0)` or a malformed `toAddress`), not an attacker action — mirroring the original report's "loss of funds for users who are not aware". No privileged actor is needed to trigger it; the friendly `sendFrom` wrapper makes the mistake easy since `to_` is a plain `address` parameter with no validation.

### Recommendation
Validate the destination address at initiation, matching the checks the destination will enforce:

```solidity
// In ProxyOFT.sendFrom and/or _debitFrom
if (to_ == address(0)) revert AddressIsNull();
```

For the raw `sendFrom`, validate `toAddress_.toAddress(0) != address(0)` inside `_debitFrom`. Optionally also pre-check `bridgedInSupply() + amount_ <= maxBridgedInSupply` semantics via a view on the destination-side cap where feasible, or document that cap-related failures are retryable via `retryMessage`.

### Proof of Concept
Foundry fork test sketch:

```solidity
// Source chain
uint256 bal = msUSD.balanceOf(alice);
proxyOFT_src.sendFrom{value: fee}(alice, DST_CHAIN_ID, address(0), bal);
// succeeds: alice's msUSD is burned, lz packet emitted

// Destination chain delivers the packet
vm.prank(lzEndpoint);
proxyOFT_dst.lzReceive(DST_CHAIN_ID, srcAddr, nonce, payload);
// NonblockingLzApp stores failedMessage because SyntheticToken._mint
// reverts MintToTheZeroAddress

// Any retry reverts identically — funds are permanently unrecoverable
vm.expectRevert();
proxyOFT_dst.retryMessage(SRC_CHAIN_ID, srcAddr, nonce, payload);
assertEq(msUSD.totalSupply(), supplyBefore - bal); // burned, never re-minted
```