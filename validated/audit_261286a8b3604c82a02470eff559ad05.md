### Title
RewardsDistributor grants instant rewards equal to full token balance to accounts never checkpointed — stale index fallback bug - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` uses `accountIndexOf[token][account] == 0` to detect a never-checkpointed account, but only backstops it when the global token index is **strictly greater** than `INITIAL_INDEX` (1e18). In the window where a token's speed has just been activated and the stored index still equals `INITIAL_INDEX`, the fallback is skipped, `_deltaIndex` equals `1e18`, and the account is credited `balance.wadMul(1e18) = balance` reward tokens — with zero elapsed time and zero legitimate accrual. This is a cached-state inconsistency analog of CVE-2019-17346: a stale "index" (like a stale TLB entry tagged with the wrong PCID) is treated as valid for a context it was never written for.

### Finding Description
- `tokenStates[token_]` for a new reward token is initialized to `{index: INITIAL_INDEX, timestamp: block.timestamp}` inside `_updateTokenSpeed` (lines 296–299). This is a governor action, not the attack.
- An account that held `DepositToken`/`DebtToken` balances **before** the token was registered has `accountIndexOf[token][account] == 0`, because `updateBeforeMintOrBurn`/`updateBeforeTransfer` short-circuit while `index == 0` (lines 175–192).
- In `_calculateTokenDelta` (lines 217–231):
  ```solidity
  if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
      _accountIndex = INITIAL_INDEX;
  }
  uint256 _deltaIndex = _tokenIndex - _accountIndex;
  _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
  ```
  When `_tokenIndex == INITIAL_INDEX` exactly, `_accountIndex` stays `0`, so `_deltaIndex == 1e18` and `_tokensDelta == balanceOf(account)`.
- `claimRewards` calls `_updateTokenIndex` first, but in the same block as speed activation `_deltaTimestamps == 0`, so `_calculateTokenIndex` returns `(0, 0)` and the index remains `INITIAL_INDEX` (lines 201–212, 271–282).
- Result: `tokensAccruedOf[account] += balanceOf(account)`, paid out in `rewardToken` by `_transferRewardIfEnoughTokens` up to the distributor's balance (lines 248–256).

### Impact Explanation
An unprivileged attacker drains the reward token reserve: each wei of deposit/debt token balance mints one wei of claimable `rewardToken` instantly. Using a flash loan, the attacker deposits a massive amount of collateral (or issues debt to hold `DebtToken` balance) in the same block the governor activates a reward speed, calls `claimRewards`, and withdraws/repays, pocketing up to the distributor's entire `rewardToken` balance — direct theft of all users' unclaimed yield.

### Likelihood Explanation
The exploit window is a single block: once any later block triggers `_updateTokenIndex`, the index exceeds `INITIAL_INDEX` and the fallback works correctly. However, `updateTokenSpeed`/`updateTokenSpeeds` transactions are publicly visible in the mempool, and the attacker can back-run them in the same block. The only prerequisites are a funded `RewardsDistributor` (normal operating state) and a flash-loanable deposit asset or self-issued debt position. `claimRewards` is `nonReentrant` but the attack needs no reentrancy; no pause flag, supply cap, or SynthContext check intervenes.

### Recommendation
Treat `_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX` (or simply `_tokenIndex > 0`) as the never-checkpointed case and set `_accountIndex = _tokenIndex` (or `INITIAL_INDEX`) unconditionally when the account has no stored index, so `_deltaIndex` is 0 for a first-touch account regardless of the current global index.

### Proof of Concept
Hardhat sketch (fork or fixture with real contracts):

```ts
// 1. Attacker deposits BEFORE the reward token speed is set.
await underlying.mint(attacker.address, DEPOSIT);          // or flash loan
await underlying.connect(attacker).approve(msdToken.address, DEPOSIT);
await msdToken.connect(attacker).deposit(DEPOSIT, attacker.address);
// accountIndexOf[msdToken][attacker] === 0

// 2. Governor activates rewards for msdToken (attacker back-runs in same block).
await rewardsDistributor.connect(governor).updateTokenSpeed(msdToken.address, SPEED);
// tokenStates[msdToken] = { index: 1e18, timestamp: now }

// 3. Same block: index is still exactly INITIAL_INDEX.
await rewardsDistributor.connect(attacker).claimRewards(attacker.address, [msdToken.address]);

// tokensDelta = balance.wadMul(1e18) = DEPOSIT
expect(await rewardToken.balanceOf(attacker.address)).to.eq(DEPOSIT); // up to distributor balance
```

The Foundry equivalent uses `vm.prank` for the governor call and asserts `rewardToken.balanceOf(attacker) == msdToken.balanceOf(attacker)` after a single `claimRewards` in the block where `updateTokenSpeed` executed.