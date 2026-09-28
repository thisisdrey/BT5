### Title
First-touch reward accrual credits the entire token balance when the global index is still at `INITIAL_INDEX` - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
The analog to the Alchemix double-accrual bug lives in `RewardsDistributor._calculateTokenDelta`. When a reward token's global index is still exactly `INITIAL_INDEX` (i.e., `updateTokenSpeed` activated a speed in the current block, before any index update has run), a first-time accrual for an account computes `deltaIndex = INITIAL_INDEX - 0 = 1e18`, crediting `tokensAccruedOf[account] += balanceOf(account) * 1` — effectively the account's entire deposit/debt token balance as reward tokens — instead of zero.

### Finding Description
`_calculateTokenDelta` only corrects a zero `accountIndexOf` when the global index has already moved past `INITIAL_INDEX` (contracts/RewardsDistributor.sol:223-230):

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

The strict `>` comparison leaves a hole: when `_tokenIndex == INITIAL_INDEX`, `_accountIndex` stays `0` and `_deltaIndex == INITIAL_INDEX` (1e18). `wadMul(balance, 1e18)` returns `balance`, so `_updateTokensAccruedOf` (lines 261-266) adds the full token balance to `tokensAccruedOf`.

Attack path (all unprivileged, via public entry points):
1. Attacker deposits collateral and holds a `DepositToken` balance (or debt balance on a `DebtToken`).
2. Governor calls `updateTokenSpeed(token, speed > 0)` for a token whose state is `{index: INITIAL_INDEX, timestamp: block.timestamp}` (lines 296-299).
3. In the **same block**, the attacker back-runs with `RewardsDistributor.updateBeforeMintOrBurn(token, attacker)` — explicitly callable by anyone (lines 175-180). `_updateTokenIndex` is a no-op because `deltaTimestamps == 0` (lines 202-211), so `tokenStates[token].index` is still `INITIAL_INDEX`, and the accrual credits `tokensAccruedOf[attacker] += attackerBalance`.
4. Attacker calls `claimRewards(attacker)`, which transfers `tokensAccruedOf[attacker]` reward tokens via `_transferRewardIfEnoughTokens` (lines 248-256), draining up to the distributor's whole reward-token balance by sizing the deposit accordingly.

The same-block requirement is satisfiable via mempool back-running/bundling of the governor's `updateTokenSpeed` transaction. Note the state is also freshly `INITIAL_INDEX` on the `else` branch of `_updateTokenSpeed` when a token's speed goes 0 → >0 while the index never advanced.

### Impact Explanation
Direct theft of unclaimed yield: the reward tokens held by `RewardsDistributor` belong to all depositors/debtors who legitimately accrued them. The attacker mints a claim equal to their deposit-token balance with zero elapsed reward time, then withdraws real reward ERC20, draining the distributor up to its full balance. This is repeatable per reward token (deposit and debt tokens) and per `RewardsDistributor` instance.

### Likelihood Explanation
Requires (a) rewards being configured (the contract exists for exactly this purpose and `updateTokenSpeed` is the documented activation path), and (b) a same-block back-run of a governor transaction — standard MEV bundling, no privileged role. Attacker capital requirement is only a deposit sized to the distributor's reward balance. No pause, cap, reentrancy guard, or `SynthContext` check intervenes: `updateBeforeMintOrBurn` is permissionless by design.

### Recommendation
Change the fallback condition to cover equality — i.e., `if (_accountIndex == 0) _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;` or simply clamp `accountIndex` to `INITIAL_INDEX` whenever it is below it — so a first accrual at the initial index yields `deltaIndex == 0`. Alternatively, have `_updateTokenSpeed` eagerly call `_updateTokensAccruedOf`-independent initialization, or revert accrual calls while `index == INITIAL_INDEX && timestamp == block.timestamp`.

### Proof of Concept
Foundry/Hardhat fork sketch:

```solidity
// setup: attacker deposits, holds msdTOKEN balance = distributor's reward balance
depositToken.deposit(amount, attacker);

// governor activates rewards for the deposit token
vm.prank(governor);
rewardsDistributor.updateTokenSpeed(address(depositToken), speed);

// SAME BLOCK: back-run — index is still INITIAL_INDEX, deltaTimestamps == 0
rewardsDistributor.updateBeforeMintOrBurn(address(depositToken), attacker);

// tokensAccruedOf[attacker] == depositToken.balanceOf(attacker) instead of ~0
assertEq(rewardsDistributor.tokensAccruedOf(attacker), depositToken.balanceOf(attacker));

rewardsDistributor.claimRewards(attacker); // drains reward token up to distributor balance
assertGt(rewardToken.balanceOf(attacker), 0);
```

Caveat: the exact value of `INITIAL_INDEX` (assumed `1e18`, consistent with `wadMul`/`wadDiv` and `DEFAULT_INDEX` usage in `test/RewardDistributor.test.ts`) was not directly verified because file lines 1-90 were not read; if it differs from 1e18 the over-credit scales proportionally but remains nonzero and exploitable.