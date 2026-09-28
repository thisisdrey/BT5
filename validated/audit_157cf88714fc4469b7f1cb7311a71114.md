### Title
Frontrunning `updateMaxBridgedInSupply()` (or innocent concurrent bridge-in) leaves `bridgedInSupply > maxBridgedInSupply`, permanently reverting all inbound bridge mints until governor intervention - (File: contracts/SyntheticToken.sol)

### Summary
`SyntheticToken.updateMaxBridgedInSupply()` lets the governor set the inbound bridging cap with no on-chain check that the new cap is `>= bridgedInSupply()`. The cap is enforced only inside `_mint()` at bridge-receive time. Between the moment the governor inspects `bridgedInSupply()` off-chain and the `updateMaxBridgedInSupply()` transaction, any unprivileged user can bridge tokens in through `ProxyOFT`, inflating `bridgedInSupply` above the cap the governor is about to set. Once `bridgedInSupply() > maxBridgedInSupply`, every subsequent inbound bridge message reverts with `SurpassMaxBridgingSupply`, freezing cross-chain transfers into the chain until the governor raises the cap again.

### Finding Description
In `SyntheticToken._mint()`, when the caller is `proxyOFT` (i.e., a LayerZero receive via `ProxyOFT._creditTo` → `SyntheticToken.mint`), the contract accumulates `totalBridgedIn` and reverts if `bridgedInSupply() > maxBridgedInSupply` (`contracts/SyntheticToken.sol:338-340`). The governor setter `updateMaxBridgedInSupply()` (`contracts/SyntheticToken.sol:414-419`) only checks `NewValueIsSameAsCurrent` — it does not enforce `maxBridgedInSupply_ >= bridgedInSupply()`:

```solidity
// contracts/SyntheticToken.sol:338-340
if (_isMsgSenderProxyOFT(_msgSender)) {
    totalBridgedIn += amount_;
    if (bridgedInSupply() > maxBridgedInSupply) revert SurpassMaxBridgingSupply();
}
```

```solidity
// contracts/SyntheticToken.sol:414-419
function updateMaxBridgedInSupply(uint256 maxBridgedInSupply_) external onlyGovernor {
    uint256 _currentMaxBridgedInBalance = maxBridgedInSupply;
    if (maxBridgedInSupply_ == _currentMaxBridgedInBalance) revert NewValueIsSameAsCurrent();
    emit MaxBridgedInSupplyUpdated(_currentMaxBridgedInBalance, maxBridgedInSupply_);
    maxBridgedInSupply = maxBridgedInSupply_;
}
```

The governor's natural procedure is identical to the Biconomy `getMaxCommunityLpPositon()` pattern: read `bridgedInSupply()` off-chain, then submit `updateMaxBridgedInSupply(newCap)` with `newCap >= bridgedInSupply`. Because bridging in is permissionless (any EOA calls `ProxyOFT.sendFrom` on a remote chain, and the LZ receive calls `mint` → `totalBridgedIn += amount_`), a user — malicious frontrunner or innocent — can raise `bridgedInSupply` between the governor's read and the cap update, or even between two pending bridge messages. The same race applies symmetrically to `updateMaxBridgedOutSupply()`/`bridgedOutSupply()` in `_burn` (`contracts/SyntheticToken.sol:275-277`) and to `updateMaxTotalSupply()`/`totalSupply` (`contracts/SyntheticToken.sol:346-347,404-409`), where a user can inflate supply via normal `Pool` issuance between read and set, bricking all minting (issuance, swaps, bridge-in) on that synth.

### Impact Explanation
When `bridgedInSupply() > maxBridgedInSupply`, every inbound `PT_SEND`/`PT_SEND_AND_CALL` receive reverts inside `_mint`. In LayerZero OFT this causes the message to fail and be stored as a failed payload; `retryMessage`/`retryOFTReceived` also reverts for the same reason, so the bridged tokens remain locked/burned on the source chain while unmintable on the destination — a temporary freezing of user funds and a complete halt of inbound bridging for that synth until the governor raises the cap. The `updateMaxTotalSupply` variant similarly halts all new issuance (debt issuance, `Pool.swap` mints, bridge-ins), degrading liveness. Recovery requires another privileged transaction, matching the accepted-severity profile of the original finding.

### Likelihood Explanation
No malicious actor is required: the invariant `maxBridgedInSupply >= bridgedInSupply` can be broken by an innocent user bridging during a routine cap-reduction operation, exactly the timing edge case described in the reference report. A deliberate attacker can also guarantee it by observing the governor's pending `updateMaxBridgedInSupply` transaction and sandwiching a bridge-in (or arranging an inbound message to land in the same block). No privileged role, oracle manipulation, or special configuration is needed — only the permissionless `ProxyOFT.sendFrom` entry point. The impact is recoverable by the governor, so severity is moderate-low, consistent with the source finding.

### Recommendation
Enforce the invariant programmatically in the setters:

```solidity
// updateMaxBridgedInSupply
if (maxBridgedInSupply_ < bridgedInSupply()) revert CapBelowCurrentSupply();

// updateMaxBridgedOutSupply
if (maxBridgedOutSupply_ < bridgedOutSupply()) revert CapBelowCurrentSupply();

// updateMaxTotalSupply
if (newMaxTotalSupply_ < totalSupply) revert CapBelowCurrentSupply();
```

This mirrors the recommended `require(_perTokenWalletCap <= getMaxCommunityLpPositon(_token))` fix — a cheap `SLOAD`-based check, since `bridgedInSupply()`/`bridgedOutSupply()` are simple storage subtractions, not the bbst-style iteration the original protocol feared.

### Proof of Concept
Hardhat-style fork test against deployed `SyntheticToken` + `ProxyOFT` (deterministic, no attacker needed — demonstrate with a benign user):

```typescript
// Setup: governor intends to reduce maxBridgedInSupply.
const cur = await msUSD.bridgedInSupply();           // e.g. 1000e18
const newCap = cur;                                  // governor picks cap == current supply

// Step 1 (governor reads bridgedInSupply() off-chain, prepares tx with newCap)
// Step 2: innocent user bridges X tokens in via ProxyOFT on remote chain.
//   On destination: ProxyOFT._creditTo -> msUSD.mint(user, X)
//   -> totalBridgedIn += X  (bridgedInSupply now 1000e18 + X, mint succeeds under old cap)
await proxyOFT.mockLzReceive(user.address, xAmount); // or real sendFrom from remote fork
expect(await msUSD.bridgedInSupply()).to.eq(cur.add(xAmount));

// Step 3: governor's updateMaxBridgedInSupply(newCap) lands
await msUSD.connect(governor).updateMaxBridgedInSupply(newCap);

// Invariant broken: bridgedInSupply (1000e18+X) > maxBridgedInSupply (1000e18)
// Step 4: ANY subsequent inbound bridge receive reverts -> LZ payload stored as failed
await expect(proxyOFT.mockLzReceive(victim.address, anyAmount))
  .to.be.revertedWithCustomError(msUSD, 'SurpassMaxBridgingSupply');

// Step 5: retrying the failed message also reverts -> victim funds stuck on source chain
await expect(proxyOFT.retryMessage(...)).to.be.reverted; // until governor raises cap
```

Analogous PoC for the symmetric cases: a user mints synths via `DebtToken.issue`/`Pool.swap` between the governor's `totalSupply()` read and `updateMaxTotalSupply()`, leaving `totalSupply > maxTotalSupply`, after which every `mint` (issuance, swap, bridge-in) reverts `SurpassMaxSynthSupply`.