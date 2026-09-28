### Title
Uninitialized `accountIndexOf` treated as index 0 lets a same-block depositor claim rewards equal to their full balance - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
The analog to `release_reference()` marking registers `SCALAR_VALUE` instead of `NOT_INIT`: a storage slot that semantically means "unset" is consumed as a real value. In `RewardsDistributor._calculateTokenDelta`, `accountIndexOf[token][account] == 0` is meant to represent "account never accrued", but it is used verbatim as a valid index when the stored token index still equals `INITIAL_INDEX`. A depositor whose index was never initialized therefore computes a delta of `1e18`, i.e., accrues reward tokens equal to 100% of their token balance, and can claim them.

### Finding Description
When a token is first given a non-zero speed, `_updateTokenSpeed` stores `TokenState({index: INITIAL_INDEX, timestamp: now})` (`contracts/RewardsDistributor.sol:296-299`). `INITIAL_INDEX = 1e18` (line 53).

`_calculateTokenDelta` (lines 217-231):

```solidity
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

The fallback to `INITIAL_INDEX` only fires when `_tokenIndex > INITIAL_INDEX` (strict). While the stored index still equals `INITIAL_INDEX` — which is true for the whole block in which `updateTokenSpeed` runs, because `_calculateTokenIndex` returns no new index when `deltaTimestamps == 0` (lines 200-212) — an account with `accountIndexOf == 0` gets:

- `_deltaIndex = 1e18 - 0 = 1e18`
- `_tokensDelta = balance.wadMul(1e18) = balance`

`updateBeforeMintOrBurn` is permissionless and callable by anyone (line 175), and `claimRewards` pays out `tokensAccruedOf` as long as the distributor holds enough `rewardToken` (`_transferRewardIfEnoughTokens`, lines 248-256).

Attack path (all unprivileged, same block as the governor's `updateTokenSpeed(token, speed>0)` tx — this tx is visible in the mempool and can be front-run/back-run):

1. Flash-loan the underlying, `deposit()` a very large amount into the `DepositToken` → attacker holds huge `balanceOf`. (Deposit before the index exists is a no-op for rewards since `tokenStates[token].index == 0`.)
2. Governor's `updateTokenSpeed` executes → `tokenStates[token] = {INITIAL_INDEX, now}`.
3. Attacker calls `updateBeforeMintOrBurn(token, attacker)` → `_updateTokenIndex` is a no-op (same block) → `_updateTokensAccruedOf` credits `tokensAccruedOf[attacker] = balance`.
4. Attacker calls `claimRewards(attacker)` → receives `balance` worth of `rewardToken`, draining the distributor up to its balance.
5. Attacker withdraws collateral and repays the flash loan.

### Impact Explanation
Direct theft of unclaimed yield: the attacker extracts `rewardToken` up to `min(distributorBalance, attackerDepositTokenBalance)`. Since deposit size is bounded only by available flash liquidity, the entire reward reserve earmarked for legitimate depositors/borrowers can be drained in one transaction.

### Likelihood Explanation
Requires a governor `updateTokenSpeed(token_, >0)` call that adds a *new* reward token (index transition 0 → `INITIAL_INDEX`) and a distributor funded with reward tokens. Such calls are routine maintenance and visible in the mempool; the attacker needs no privileges — `updateBeforeMintOrBurn`, `claimRewards`, `deposit` are all public. Note the window is tight (same block, before any time-elapsed index update), which lowers likelihood to Medium/Low, but MEV/backrunning makes it reliably capturable on L2s with public mempools and on chains where the keeper/governor tx can be observed.

### Recommendation
Treat index 0 as "not initialized" in all cases, not only when `_tokenIndex > INITIAL_INDEX`:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

or equivalently initialize `accountIndexOf` to `INITIAL_INDEX` on first accrual regardless of the token index value. Additionally, consider initializing the account index inside `updateBeforeMintOrBurn`/`updateBeforeTransfer` using the token index at *first touch*, so zero-state can never be interpreted as a real index.

### Proof of Concept
Hardhat-style sequence (deployed config: pool, `msdX` DepositToken with underlying `X`, funded `RewardsDistributor`):

```ts
// 1. Attacker flash-borrows/mints X and deposits (index still 0 → rewards no-op)
await x.mint(attacker, LARGE)
await x.connect(attacker).approve(msdX.address, LARGE)
await msdX.connect(attacker).deposit(LARGE, attacker.address)

// 2. Same block: governor adds msdX as reward token
await rewardsDistributor.connect(governor).updateTokenSpeed(msdX.address, SPEED)
// tokenStates[msdX].index == INITIAL_INDEX (1e18) now

// 3. Public accrual: _deltaIndex = 1e18 - 0 → tokensDelta == attacker balance
await rewardsDistributor.connect(attacker).updateBeforeMintOrBurn(msdX.address, attacker.address)
expect(await rewardsDistributor.tokensAccruedOf(attacker.address)).eq(LARGE)

// 4. Drain
await rewardsDistributor.connect(attacker)['claimRewards(address)'](attacker.address)
expect(await rewardToken.balanceOf(attacker.address)).eq(min(LARGE, distributorBalance))
```

Caveats not fully verified within this pass: the exact on-chain `tokenSpeeds`/reward-token balances per deployed pool, and whether governance batches `updateTokenSpeed` privately (e.g., via multisig + private mempool), which would reduce practical exploitability but does not remove the incorrect-state defect.