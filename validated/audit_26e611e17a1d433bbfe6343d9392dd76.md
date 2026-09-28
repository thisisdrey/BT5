### Title
Accounts holding reward tokens before a speed is set can claim `balance * INITIAL_INDEX` worth of rewards in the same block the speed is enabled - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` contains a flawed fallback for accounts that were never indexed: it only substitutes `INITIAL_INDEX` (1e18) when the current token index is *strictly greater* than `INITIAL_INDEX`. When the token index equals `INITIAL_INDEX` — which is exactly the state right after a reward token is first enabled via `updateTokenSpeed`/`updateTokenSpeeds` — a holder with `accountIndexOf == 0` computes `_deltaIndex = 1e18 - 0 = 1e18` instead of `1e18 - 1e18 = 0`, so `_tokensDelta = balanceOf(account).wadMul(1e18)` = their full token balance is credited to `tokensAccruedOf` and paid out by `claimRewards`. This is the same bug class as CVE-2016-9754: an integer boundary/comparison miscalculation that inflates a computed amount.

### Finding Description
When a token's speed is first set to a non-zero value, `_updateTokenSpeed` initializes its state to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` and pushes it into `tokens`. Thereafter `updateBeforeMintOrBurn`, `updateBeforeTransfer`, and `claimRewards` only act when `tokenStates[token_].index > 0`.

In `_calculateTokenDelta`:

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

If `_tokenIndex == INITIAL_INDEX` and `_accountIndex == 0`, the fallback is skipped and `_deltaIndex == 1e18`. `_tokenIndex` equals `INITIAL_INDEX` whenever `_calculateTokenIndex` produces no new index — i.e., in the same block/timestamp as the `updateTokenSpeed` call (`_deltaTimestamps == 0` leaves the stored index at 1e18).

An account has `accountIndexOf == 0` plus a non-zero balance whenever it received its `DepositToken`/`DebtToken` balance *before* the token was added to the distributor — while `index == 0`, `updateBeforeMintOrBurn`/`updateBeforeTransfer` early-return and never write `accountIndexOf`. So the exploit sequence is:

1. Attacker deposits collateral (holds `DepositToken` balance, `accountIndexOf == 0` because the token is not yet reward-enabled).
2. Governor calls `updateTokenSpeeds`/`updateTokenSpeed` enabling rewards for that token (a routine operation) → `index = 1e18`, `timestamp = now`.
3. In the same block, the attacker (or anyone calling `claimRewards` cannot — the payout goes to `account_`, so the attacker calls it for themselves) calls `claimRewards(attacker)` or simply performs a `transfer`/`withdraw` which triggers `updateBeforeMintOrBurn` → `_deltaIndex = 1e18` → `tokensAccruedOf[attacker] += attackerBalance` → `claimRewards` pays `min(accrued, distributorBalance)` of `rewardToken` to the attacker.

The `_updateTokenIndex` call inside `claimRewards` does not help: with `_deltaTimestamps == 0` it returns `(0,0)` and leaves the stored index at `1e18`.

### Impact Explanation
The attacker is credited `balanceOf(attacker) * 1` reward tokens instantly, with no time elapsed — i.e., they steal the `RewardsDistributor`'s `rewardToken` balance up to the size of their deposit-token position. This is theft of unclaimed yield/reward funds belonging to other users (the distributor holds pooled reward tokens). Using a flash-borrowed or leveraged position (e.g., via `SmartFarmingManager.leverage`, which mints `DepositToken`s to the user) amplifies the credited amount far beyond the attacker's capital. No privilege is required: `claimRewards`, `deposit`, `transfer`, and `withdraw` are all public, and the only external precondition is a routine governor `updateTokenSpeed`/`updateTokenSpeeds` call, which the attacker can monitor in the mempool and back-run in the same block.

### Likelihood Explanation
Likelihood is moderate: it requires (a) a reward token being newly enabled or re-enabled at `index == INITIAL_INDEX`, and (b) the attacker acting in that same block. Both are achievable — governor speed updates are normal operational events, and mempool observation plus same-block execution is standard. The bug is deterministic once those conditions hold: the boundary condition `> INITIAL_INDEX` instead of `>=` unconditionally grants `wadMul(balance, 1e18)` to any never-indexed holder. It does not depend on oracle manipulation, malicious governance, or privileged actors.

### Recommendation
Change the fallback so that a never-indexed account is always treated as starting at `INITIAL_INDEX` whenever the token index is initialized, e.g.:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

or equivalently `_accountIndex = INITIAL_INDEX` whenever `accountIndexOf` is unset and `tokenStates.index != 0`. Additionally, when a token is first enabled in `_updateTokenSpeed`, consider recording `accountIndexOf` lazily at `INITIAL_INDEX` semantics consistently, and add a regression test asserting zero delta for pre-existing holders at the enabling timestamp.

### Proof of Concept
Sketch (Foundry-style, using the repo's mocks; a Hardhat test against `test/RewardDistributor.test.ts` scaffolding works the same):

```solidity
// Setup: ERC20Mock rewardToken, ERC20Mock collateral, MasterOracleMock,
// FeeProvider(0 fees), Pool, DepositToken(collateral), RewardsDistributor(pool, rewardToken)
// governor adds distributor via pool.addRewardsDistributor(rd)

uint256 amount = 1_000e18;
collateral.mint(user, amount);
collateral.approve(depositToken, amount);
// 1. Deposit BEFORE any speed is set (index == 0 -> no accountIndex write)
depositToken.deposit(amount, user);
assertEq(rd.accountIndexOf(depositToken, user), 0);

// 2. Fund distributor and enable speed (governor)
rewardToken.mint(address(rd), 1_000_000e18);
vm.prank(governor);
rd.updateTokenSpeed(IERC20(address(depositToken)), 1e18); // index := 1e18, timestamp := now

// 3. Same block: claim rewards -> deltaIndex = 1e18, tokensDelta = balance
vm.prank(user);
rd.claimRewards(user);

// Expected (buggy): user receives min(amount, rdBalance) = 1_000e18 rewardToken
assertEq(rewardToken.balanceOf(user), amount); // stole yield with 0 elapsed time
```

A multi-account variant shows `claimRewards([user], [token])` also triggers it, and `claimable(user)` returns `amount` pre-execution, confirming the inflated accrual.