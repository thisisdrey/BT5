### Title
Uninitialized `accountIndexOf` grants instant rewards equal to full balance when a reward token is freshly added - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` uses `accountIndexOf[token][account]` directly. An uninitialized account index (0) is only corrected to `INITIAL_INDEX` when the token's global index is strictly greater than `INITIAL_INDEX`. During the window where `tokenStates[token_].index == INITIAL_INDEX` — i.e., the same block in which `updateTokenSpeed` first registers the token — any account that holds a balance of a tracked DepositToken/DebtToken but was never accrued receives `tokensDelta = balance * INITIAL_INDEX = balance`, stealing reward tokens.

### Finding Description
The analog to CVE-2021-30578 (use of uninitialized memory) is the use of the uninitialized `accountIndexOf` entry in `_calculateTokenDelta`:

```solidity
// contracts/RewardsDistributor.sol:217-231
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a new reward token is registered via `_updateTokenSpeed`, its `TokenState` is set to `{index: INITIAL_INDEX, timestamp: block.timestamp}` and it is pushed to `tokens` (contracts/RewardsDistributor.sol:296-299). Any account that held DepositToken/DebtToken balances before registration still has `accountIndexOf == 0`, because `updateBeforeMintOrBurn`/`updateBeforeTransfer` skip updates while `tokenStates[token_].index == 0` (line 187).

If, in that same block (`block.timestamp == _supplyState.timestamp`, so `_updateTokenIndex` leaves `index == INITIAL_INDEX`), anything triggers `_updateTokensAccruedOf` for that account, the guard `_tokenIndex > INITIAL_INDEX` is false, `_accountIndex` stays 0, and:

```
_deltaIndex = 1e18 - 0 = 1e18
_tokensDelta = balance.wadMul(1e18) = balance
```

`tokensAccruedOf[account]` is set to the account's full deposit/debt balance (contracts/RewardsDistributor.sol:261-266). `claimRewards` then transfers `rewardToken` up to that amount via `_transferRewardIfEnoughTokens` (lines 248-256), which is callable by anyone for any account.

Entry points (all public, attacker-reachable):
- `RewardsDistributor.updateBeforeMintOrBurn(token, attacker)` — permissionless per devdoc.
- `DepositToken.transfer` / `transferFrom`, `deposit`, `withdraw` — via `updateRewardsBeforeTransfer`/`updateRewardsBeforeMintOrBurn` (contracts/DepositToken.sol:124-144).
- `DebtToken` mint/burn paths — same modifier (contracts/DebtToken.sol:114-121).

### Impact Explanation
A pre-existing depositor (or an attacker who deposited while the reward token was unregistered) can claim reward tokens equal to their full DepositToken/DebtToken balance immediately upon token registration, with zero time-weighted accrual. This directly drains the distributor's `rewardToken` holdings — tokens reserved for legitimate emissions — constituting theft of unclaimed yield belonging to all other users. With a large deposit (e.g., via flash-loan-funded deposit in a prior block, or simply a whale position), the attacker extracts the distributor's entire reward balance.

### Likelihood Explanation
Requirements:
- A reward token that was never registered (or whose `tokenStates.index` was reset — re-registration after speed removal does not reset `index`, so only the first registration path applies).
- The trigger must execute in the same block/timestamp as the `updateTokenSpeed`/`updateTokenSpeeds` call that sets `index = INITIAL_INDEX`, before any later-block index update.

The attacker cannot call `updateTokenSpeed` themselves (governor/`tokenSpeedKeeper` only), but they can pre-position: deposit collateral while `index == 0` (the update hooks no-op, leaving `accountIndexOf == 0`), then front-run/back-run the public `updateTokenSpeed` transaction in the same block with `updateBeforeMintOrBurn` + `claimRewards`. On chains with public mempools this is a standard sandwich; the window also persists for the remainder of the block timestamp. No governance action, malicious oracle, or privileged role is needed on the attacker's side.

### Recommendation
Treat an unset `accountIndexOf` as the current token index (or `INITIAL_INDEX`) unconditionally, not only when `_tokenIndex > INITIAL_INDEX`:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? _tokenIndex : INITIAL_INDEX;
    // or simply: _accountIndex = _tokenIndex (>= INITIAL_INDEX whenever index > 0)
}
```

Equivalently, when `_accountIndex == 0` return early with `_tokensDelta = 0` if `_tokenIndex <= INITIAL_INDEX`. Alternatively, at registration time in `_updateTokenSpeed`, proactively settle indexes — though the per-account lazy fix above is cheaper and sufficient.

### Proof of Concept
Hardhat-style reproduction (assuming distributor already funded with `rewardToken`):

```ts
// Setup: attacker deposits while reward token is NOT registered
// (updateBeforeMintOrBurn no-ops because tokenStates.index == 0)
await depositToken.deposit(depositAmount, attacker.address);
expect(await rewardsDistributor.accountIndexOf(msdToken.address, attacker.address)).eq(0);

// Same block as governor's updateTokenSpeed:
await ethers.provider.send('evm_setAutomine', [false]);
await rewardsDistributor.connect(governor).updateTokenSpeed(msdToken.address, speed); // index = 1e18
// Attacker settles their (uninitialized) index and claims:
await rewardsDistributor.updateBeforeMintOrBurn(msdToken.address, attacker.address);
await ethers.provider.send('evm_mine');

expect(await rewardsDistributor.tokensAccruedOf(attacker.address))
    .eq(await msdToken.balanceOf(attacker.address)); // full balance, zero time elapsed

await rewardsDistributor.claimRewards(attacker.address); // drains rewardToken
```

A Foundry fork variant: `vm.prank(governor)` the `updateTokenSpeed` call, then `vm.roll`/`vm.warp` to the same timestamp, call `updateBeforeMintOrBurn` and `claimRewards`, and assert the reward token balance moved equals the attacker's deposit balance.