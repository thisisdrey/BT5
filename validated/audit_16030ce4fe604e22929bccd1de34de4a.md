### Title
Missing per-account index treated as zero instead of `INITIAL_INDEX` lets any holder instantly claim `balance`-sized rewards - ([File: contracts/RewardsDistributor.sol])

### Summary
`RewardsDistributor._calculateTokenDelta` corrects a missing `accountIndexOf` entry (the "stripped"/absent per-account metadata, analogous to Lua 5.4.0 dereferencing debug info that doesn't exist) only when the token index is strictly greater than `INITIAL_INDEX`. When `tokenStates[token].index == INITIAL_INDEX` exactly — i.e., reward speed was configured but the index has not yet grown — a zero account index is used verbatim, producing `_deltaIndex = 1e18` and crediting the account `balanceOf(account)` reward tokens out of thin air. Any unprivileged user can trigger this through the permissionless `updateBeforeMintOrBurn`/`updateBeforeTransfer` and then drain the distributor via `claimRewards`.

### Finding Description
In `contracts/RewardsDistributor.sol:217-231`:

```solidity
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

The fallback exists to prevent an account whose index was never recorded from being credited rewards for the entire index history. But it is gated on `_tokenIndex > INITIAL_INDEX`. `tokenStates[token_]` is initialized to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` in `_updateTokenSpeed` (lines 296-299). During the window where `index == INITIAL_INDEX` — trivially, the same block/timestamp in which the governor's `updateTokenSpeed`/`syncTokenSpeed` transaction executes, since `_calculateTokenIndex` returns zeros when `_deltaTimestamps == 0` (lines 202-211) — the correction never applies and `_deltaIndex = 1e18 - 0 = 1e18`.

Attack trace (no privileged role needed):

1. Attacker deposits collateral into a `DepositToken` (or holds `DebtToken` balance) *before* rewards are enabled — `updateBeforeMintOrBurn` is a no-op while `tokenStates[token].index == 0` (line 176), so `accountIndexOf[token][attacker]` stays 0.
2. Governor submits the routine `updateTokenSpeed(token, speed > 0)` (or `updateTokenSpeeds` batch / keeper's `syncTokenSpeed`) transaction. In the same block, the attacker calls the permissionless `RewardsDistributor.updateBeforeMintOrBurn(token, attacker)` (explicitly documented as callable by anyone, line 173). `_updateTokenIndex` no-ops (`_deltaTimestamps == 0`), then `_calculateTokenDelta` yields `_tokensDelta = balance.wadMul(1e18) = balance`.
3. Attacker calls `claimRewards(attacker)` → `_transferRewardIfEnoughTokens` transfers `balance` units of `rewardToken` from the distributor (lines 248-256).

The attacker can first inflate `balanceOf` via `Pool.deposit`/`SmartFarmingManager.leverage` with a flash-loaned collateral to maximize `_tokensDelta`. No modifier stops this: `updateBeforeMintOrBurn` has no access control, no `nonReentrant`, no pause check.

### Impact Explanation
Theft of unclaimed yield / direct theft of distributor-held `rewardToken`. The distributor's reward balance funds all users' accrued rewards; minting `balanceOf(attacker)` worth of `tokensAccruedOf` lets the attacker drain up to the full reward balance, and every legitimately accrued claim behind them becomes unpayable (`_transferRewardIfEnoughTokens` silently skips underfunded payouts, permanently freezing other users' yield).

### Likelihood Explanation
The trigger condition (`index == INITIAL_INDEX` while a non-indexed account holds token balance) occurs deterministically in the same block any reward token is (re)activated — a routine governance/keeper operation. Because `updateBeforeMintOrBurn` is permissionless, the attacker needs only to back-run that transaction in the same block (or use `Operator.execute` to bundle calls), plus a pre-existing token balance obtainable via a flash-loan-funded deposit. Deployed distributors exist on mainnet/optimism (`deployments/mainnet/MetRewardsDistributor.json`, `deployments/optimism/RewardsDistributor.json`).

### Recommendation
In `_calculateTokenDelta`, treat a missing account index as the current global index (zero accrued delta) rather than only as `INITIAL_INDEX`:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex; // or INITIAL_INDEX floored at _tokenIndex
}
```

Alternatively, initialize `accountIndexOf[token_][account_]` to `INITIAL_INDEX` whenever a token is activated, and gate `claimRewards`/`_updateTokensAccruedOf` on `_tokenIndex > INITIAL_INDEX` consistently.

### Proof of Concept
Hardhat fork sketch (mainnet), assuming a pool with `msdWETH` DepositToken and `MetRewardsDistributor`:

```ts
// 1. Before rewards activated: attacker deposits WETH, receives msdWETH
await weth.approve(msdWETH.address, amount);
await msdWETH.deposit(amount); // accountIndexOf[msdWETH][attacker] stays 0

// 2. Same block as governor's updateTokenSpeed(msdWETH, speed):
//    tokenStates[msdWETH] = {index: 1e18, timestamp: now}
await distributor.connect(governor).updateTokenSpeed(msdWETH.address, speed);
await ethers.provider.send('evm_setAutomine', [false]); // keep same timestamp
await distributor.updateBeforeMintOrBurn(msdWETH.address, attacker.address);

// 3. tokensAccruedOf[attacker] == balanceOf(attacker)
expect(await distributor.tokensAccruedOf(attacker.address)).to.eq(
  await msdWETH.balanceOf(attacker.address)
);

// 4. Drain reward token
await distributor.claimRewards(attacker.address);
```

Alternatively, in Foundry: deploy `RewardsDistributor`, a mock `DepositToken` returning nonzero `balanceOf`, call `updateTokenSpeed` then `updateBeforeMintOrBurn` in the same block, and assert `tokensAccruedOf == balance`.