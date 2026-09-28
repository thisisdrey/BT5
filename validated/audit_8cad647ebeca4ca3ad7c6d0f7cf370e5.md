### Title
Stale/uninitialized reward index lets an attacker claim reward tokens equal to their full deposit balance - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` only substitutes `INITIAL_INDEX` for a zero `accountIndexOf` when the token index has *grown past* `INITIAL_INDEX`. When a token's index still equals `INITIAL_INDEX` (speed was just set and no accrual window has elapsed), a holder's `_deltaIndex` becomes `1e18`, so `_tokensDelta = balance.wadMul(1e18) = balance`, minting an instant claimable reward equal to the user's entire deposit-token balance. This mirrors the UAF bug class: state (the per-account index) is dereferenced before it was ever initialized for the account.

### Finding Description
In `contracts/RewardsDistributor.sol`:

- `_updateTokenSpeed` initializes a new token as `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` (lines 296-299).
- `_calculateTokenDelta` (lines 217-231):
  ```solidity
  uint256 _accountIndex = accountIndexOf[token_][account_];
  if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
      _accountIndex = INITIAL_INDEX;
  }
  uint256 _deltaIndex = _tokenIndex - _accountIndex;
  _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
  ```
  The guard uses a strict `>` comparison. When `_tokenIndex == INITIAL_INDEX` exactly, `_accountIndex` stays `0`, producing `_deltaIndex = 1e18`.
- `_updateTokenIndex` only writes a new index when `_deltaTimestamps > 0 && _speed > 0` (lines 200-212, 271-282). If `claimRewards` runs in the same block/timestamp as the `updateTokenSpeed(s)` transaction, the index remains `INITIAL_INDEX`.

Attack path (unprivileged, public entry points only):
1. Attacker deposits collateral and holds a `DepositToken` balance (any amount ≥ the distributor's `rewardToken` balance maximizes theft).
2. Governor schedules `updateTokenSpeed(depositToken, speed > 0)` — a routine, expected operation.
3. In the same block, attacker calls `claimRewards(attacker, [depositToken])` (public, `nonReentrant` only). `_updateTokenIndex` is a no-op (`_deltaTimestamps == 0`), `_updateTokensAccruedOf` credits `tokensAccruedOf[attacker] += balance`, and `_transferRewardIfEnoughTokens` pays out the distributor's whole `rewardToken` balance.

No modifier stops this: `claimRewards` requires nothing, `updateBeforeMintOrBurn`/`updateBeforeTransfer` are irrelevant, and `onlyIfDistributorExists`/`onlyIfTokenExists` only gate governor speed updates.

### Impact Explanation
Direct theft of the entire `rewardToken` balance held by the `RewardsDistributor` — i.e., theft of unclaimed yield belonging to all other users — in a single transaction, proportional to the attacker's deposit balance.

### Likelihood Explanation
Requires back-running a governor `updateTokenSpeed`/`updateTokenSpeeds` transaction in the same block (same `block.timestamp`). On chains with public mempools and any scheduled speed change, this is a routine MEV back-run. The attacker only needs a pre-existing deposit balance.

### Recommendation
Change the fallback to a non-strict comparison so a zero account index is always initialized: `if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) { _accountIndex = _tokenIndex; }`, or initialize `accountIndexOf` at first touch to the current token index regardless of accrual.

### Proof of Concept
Hardhat sketch (fork or fixture with `Pool`, `DepositToken`, `RewardsDistributor`, reward token funded to distributor):

```ts
// attacker deposits collateral, holds msdTOKEN balance B >= distributor reward balance
await depositToken.connect(attacker).deposit(amount);

// governor enables speed for that deposit token
await distributor.connect(governor).updateTokenSpeed(depositToken.address, speed);

// same block (automine / next tx same timestamp): claim
await distributor.connect(attacker).claimRewards(attacker.address, [depositToken.address]);

// assert: attacker received ~B reward tokens (distributor drained)
expect(await rewardToken.balanceOf(attacker.address)).to.eq(distributorBalance);
```

Key assertion: `tokensAccruedOf[attacker]` equals `B.wadMul(1e18) = B` because `accountIndexOf` stayed `0` while `tokenStates.index == INITIAL_INDEX`.