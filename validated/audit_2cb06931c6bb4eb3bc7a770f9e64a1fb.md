### Title
RewardsDistributor credits first-depositor with `balance × INITIAL_INDEX` when the supply index has not yet advanced past `INITIAL_INDEX` - ([File: contracts/RewardsDistributor.sol])

### Summary
Analogous to the kernel bug (a special-case path leaves the "cursor" — here the per-account reward index — unset, and the consumer walks it anyway), `RewardsDistributor._calculateTokenDelta` treats `accountIndexOf == 0` safely only when the global supply index has already grown beyond `INITIAL_INDEX`. When `tokenStates[token].index == INITIAL_INDEX` (no accrual tick has advanced it yet), the `INITIAL_INDEX` fallback is skipped and the zero-valued account index is used verbatim, producing `_deltaIndex = INITIAL_INDEX` and crediting the account `balance × 1e18` worth of accrued rewards instantly.

### Finding Description
`_updateTokensAccruedOf` is invoked from `updateBeforeMintOrBurn` / `updateBeforeTransfer`, which `DepositToken` and `DebtToken` call before every mint, burn, and transfer (DepositToken.sol lines 124–144). It relies on `_calculateTokenDelta`:

```solidity
// contracts/RewardsDistributor.sol
_accountIndex = accountIndexOf[token_][account_];
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a token's speed is set via `updateTokenSpeed`/`updateTokenSpeeds`/`syncTokenSpeed`, `tokenStates[token]` is initialized to `{index: INITIAL_INDEX, timestamp: now}` (lines 294–299). If a user interacts (deposit, transfer, mint) in a block before `block.timestamp` has advanced past that timestamp — or before any accrual has pushed the index above `INITIAL_INDEX` — `_updateTokenIndex` is a no-op (`_deltaTimestamps == 0`), `_tokenIndex` stays `INITIAL_INDEX`, the `> INITIAL_INDEX` guard fails, `_accountIndex` remains `0`, and `_tokensDelta = balance.wadMul(1e18) = balance`. The account's `tokensAccruedOf` is inflated by its full token balance before a single unit of reward legitimately accrued.

### Impact Explanation
`claimRewards` transfers `rewardToken` (e.g., MET/OP reward token held by the distributor) up to `tokensAccruedOf[account_]` to the caller — any account can be claimed for (line 141, `claimRewards(account_, tokens_)`). An attacker who front-runs (or simply lands in the same block as) the governor's `updateTokenSpeeds` or the keeper's `syncTokenSpeed` call deposits into the tracked `DepositToken`, is immediately credited `tokensAccruedOf == deposit balance`, and calls `claimRewards` to pull that amount of real reward tokens out — theft of unclaimed yield belonging to all other depositors. A second window exists on `DebtToken`/`DepositToken` `updateBeforeTransfer` for any account whose `accountIndexOf` was never written while the token index still equals `INITIAL_INDEX`.

### Likelihood Explanation
Requires no privileges: the trigger is a same-block/same-timestamp deposit after a speed is (re)activated on a token whose index was reset or newly initialized. `syncTokenSpeed` is keeper-called but publicly re-triggerable on every Vesper reward-rate change, and `tokenStates[token].index == INITIAL_INDEX` persists whenever accrual has produced zero `ratio` (e.g., `_tokensAccrued` rounding to 0 against a large supply, line 206–208), so the window is wider than a single block. Reward distributors are part of the deployed configuration (`pool.getRewardsDistributors()` loop runs on every deposit/mint), so this is reachable via the public `DepositToken.deposit` / `DebtToken.issue` / `transfer` entry points.

### Recommendation
Mirror the kernel fix — check the special case everywhere the value is consumed: in `_calculateTokenDelta`, treat `_accountIndex == 0` as `INITIAL_INDEX` whenever `_tokenIndex >= INITIAL_INDEX` (or whenever `tokenStates[token_].index > 0`), not only when strictly greater:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;
}
```

This makes the per-account index effectively initialized at the current supply index, so no delta is credited for state the account never earned.

### Proof of Concept
Foundry-style sketch (reproducible against the repo's own mocks, as in `test/foundry/DepositToken.invariants.t.sol`):

```solidity
// PoolRegistry + Pool + DepositToken(msdDAI) + RewardsDistributor deployed and
// distributor registered via pool.addRewardsDistributor(distributor)
// rewardToken funded into distributor: rewardToken.mint(address(distributor), 1_000_000e18)

address attacker = address(0xA11CE);

// 1) Governor (or keeper via syncTokenSpeed) activates rewards for msdDAI.
//    tokenStates[msdDAI] = {index: 1e18, timestamp: block.timestamp}
vm.prank(governor);
distributor.updateTokenSpeed(msdDAI, 10e18);

// 2) SAME timestamp: attacker deposits; updateBeforeMintOrBurn runs while
//    index is still INITIAL_INDEX.
underlying.mint(attacker, 500_000e18);
vm.startPrank(attacker);
underlying.approve(address(msdDAI), 500_000e18);
msdDAI.deposit(500_000e18, attacker);

// _calculateTokenDelta: accountIndex==0, tokenIndex==1e18 (not > 1e18)
// => deltaIndex = 1e18 => tokensAccruedOf[attacker] = 500_000e18 * 1 = 500_000e18
assertEq(distributor.claimable(attacker), 500_000e18);

// 3) Claim drains reward tokens the attacker never accrued.
distributor.claimRewards(attacker);
assertEq(rewardToken.balanceOf(attacker), 500_000e18);
```

The deposit amount is arbitrary — the stolen amount scales linearly with the attacker's deposit size, capped only by the distributor's `rewardToken` balance (`_transferRewardIfEnoughTokens`, line 248).