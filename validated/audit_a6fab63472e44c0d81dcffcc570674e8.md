### Title
RewardsDistributor credits `tokensAccruedOf` equal to the full token balance when `accountIndexOf` is unset and the global index is still `INITIAL_INDEX` - (File: contracts/RewardsDistributor.sol)

### Summary
When a tracked token's global `index` equals `INITIAL_INDEX` (i.e., no reward accrual period has elapsed yet, or speed was set but `block.timestamp` hasn't advanced past the stored timestamp), `_calculateTokenDelta` skips the `INITIAL_INDEX` fallback because the check is strictly `_tokenIndex > INITIAL_INDEX`. The delta index becomes `1e18` instead of `0`, so `_tokensDelta = balance.wadMul(1e18) = balance`. Any holder of a freshly registered DepositToken/DebtToken can claim reward tokens equal to their entire tracked balance. This mirrors CVE-2020-27194's class: a boundary value (`INITIAL_INDEX`) is treated inconsistently between the "tracking" branch and the arithmetic branch.

### Finding Description
In `_calculateTokenDelta` (contracts/RewardsDistributor.sol:217-231):

```solidity
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When `tokenStates[token_].index == INITIAL_INDEX` (the value set in `_updateTokenSpeed` at registration, contracts/RewardsDistributor.sol:298) and `accountIndexOf` is unset (0):

- The condition `_tokenIndex > INITIAL_INDEX` is false, so `_accountIndex` stays `0`.
- `_deltaIndex = INITIAL_INDEX - 0 = 1e18`.
- `_tokensDelta = balance.wadMul(1e18) = balance` (wadMul multiplies by the operand and divides by `1e18`).

The correct delta should be `0` — the account has accrued nothing since registration.

Reachability via `claimRewards` (line 150-168): for each token with `tokenStates[_token].index > 0`, it calls `_updateTokenIndex` then `_updateTokensAccruedOf`. `_updateTokenIndex`/`_calculateTokenIndex` (lines 197-212) only bumps the index when `block.timestamp > timestamp` *and* speed > 0. So the stale `INITIAL_INDEX` persists whenever:

1. The governor registers a token (`updateTokenSpeed` with `newSpeed > 0`) and, in the same block (or before the first accrual block passes), an attacker calls `claimRewards` — `deltaTimestamps == 0`, index stays `1e18`.
2. `syncTokenSpeed` is called, or speed is later set to 0 and back — any window where index remains `INITIAL_INDEX`.

The attacker needs a nonzero `balanceOf` on the tracked DepositToken/DebtToken, obtainable by a normal `deposit`/`issue` (or flash-loan-funded deposit) in the same transaction. `_updateTokensAccruedOf` (line 264) then sets `tokensAccruedOf[attacker] = fullBalance`, and `_transferRewardIfEnoughTokens` (lines 248-256) transfers `rewardToken` if the contract holds enough.

The fallback branch exists precisely to handle "account seen before registration" (set to `INITIAL_INDEX`, delta 0), but the strict `>` comparison leaves the "account unseen and index still initial" case unhandled — an inconsistent boundary/bounds treatment analogous to `scalar32_min_max_or` mishandling bounds for 64-bit values.

### Impact Explanation
Theft of unclaimed yield: the attacker extracts reward tokens (e.g., MET/esMET or Vesper rewards held by the distributor) proportional to their deposit/debt balance, draining rewards owed to other depositors/borrowers. With a flash-loan-funded deposit sized at or above the distributor's reward balance, the entire accrued reward pool can be stolen in one `claimRewards` call. No privileged role is required — the attacker only needs timing (immediately after token registration or during any `index == INITIAL_INDEX` window) plus a balance on the tracked token.

### Likelihood Explanation
The window is narrow but recurring: every time `tokenSpeeds` for a token transitions from unset to set, or speed is 0 while index never advanced (e.g., speed set to 0 before any accrual), `index == INITIAL_INDEX` holds. `syncTokenSpeed` (keeper-driven, lines 333-348) re-registers tokens via `_updateTokenSpeed` whenever `periodFinish` passes, recreating the window each Vesper rewards period. An attacker can monitor for `updateTokenSpeed(s)`/`syncTokenSpeed` transactions and front-run or back-run them with deposit + claim. Deposits carry a `depositFee`, but the payoff is the entire distributor reward balance.

### Recommendation
Change the fallback condition to `>=`:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Or equivalently, when `accountIndexOf` is 0, always initialize it to the current token index so the delta is 0. Alternatively, set `accountIndexOf` in `_updateTokenSpeed` flows isn't possible per-account, so fixing the boundary comparison in `_calculateTokenDelta` (and reviewing `_calculateInterestAccrual`-style index math in `DebtToken` for the same off-by-boundary pattern) is the minimal fix.

### Proof of Concept
Foundry/Hardhat test sketch:

```solidity
// Setup: pool, depositToken (e.g. msUSD-USDC), rewardsDistributor initialized
// with rewardToken = MET. Fund distributor with 1_000_000e18 MET.

// 1. Governor registers the deposit token with a speed:
vm.prank(governor);
rewardsDistributor.updateTokenSpeed(depositToken, 1e18); // index = INITIAL_INDEX (1e18), timestamp = now

// 2. Same block: attacker deposits collateral (flash-loaned if desired)
uint256 depositAmount = 500_000e18; // >= MET balance of distributor
underlying.approve(address(depositToken), depositAmount);
depositToken.deposit(depositAmount, attacker);

// 3. Attacker claims — index is still INITIAL_INDEX
uint256 metBefore = MET.balanceOf(attacker);
rewardsDistributor.claimRewards(attacker);
uint256 metAfter = MET.balanceOf(attacker);

// tokensDelta = depositToken.balanceOf(attacker).wadMul(INITIAL_INDEX - 0)
//             = depositToken.balanceOf(attacker)
assertEq(metAfter - metBefore, depositToken.balanceOf(attacker));
// => attacker received reward tokens equal to their full deposit balance,
//    with zero elapsed accrual time.
```

Key invariant broken: reward conservation — `sum(tokensAccruedOf)` exceeds `tokenSpeed * elapsed`, allowing extraction of rewards never emitted.