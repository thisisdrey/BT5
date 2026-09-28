### Title
`DebtToken.accrueInterest` inflates `totalSupply` without updating the rewards index, diluting reward accrual for all DebtToken reward recipients - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
In `RewardsDistributor`, every change to a reward-bearing token's supply or balances is supposed to be preceded by an index checkpoint (`updateBeforeMintOrBurn`/`updateBeforeTransfer`). `DebtToken.accrueInterest()` permanently increases `totalSupply_` (the value returned by `totalSupply()`) without any such checkpoint. The next time `_updateTokenIndex` runs, it distributes the rewards accrued over the whole elapsed period across the enlarged supply, so the index — and every holder's claimable yield — is systematically understated. This is the exact analog of the reported bug class: a code path that mutates the "staked" supply used for reward-rate math without first updating the reward rate/index.

### Finding Description
`DebtToken.accrueInterest()` is a public, unprivileged function that adds accrued interest directly to `totalSupply_` (lines 156–180). The `updateRewardsBeforeMintOrBurn` modifier exists on `_mint`/`_burn` (lines 114–121, 525, 577), but `accrueInterest` writes `totalSupply_ += _interestAmountAccrued` outside of any mint/burn path, so no rewards distributor is notified.

`RewardsDistributor._calculateTokenIndex` computes the per-token accrual as `_deltaTimestamps * tokenSpeeds[token_] / token_.totalSupply()` (contracts/RewardsDistributor.sol lines 201–207). Because `DebtToken.totalSupply()` returns `totalSupply_ + pending interest`, each interest accrual between two index updates shrinks the index delta for the entire elapsed interval — the index should have grown at the smaller supply in effect during most of that window. All accounts holding the DebtToken (used for reward distribution via `balanceOf`) receive proportionally less `tokensDelta` in `_calculateTokenDelta` (lines 229–230) and `_updateTokensAccruedOf` (lines 261–265).

An attacker can also call `accrueInterest()` directly to force the supply inflation just before any victim's `updateBeforeMintOrBurn`/`claimRewards`/`issue`/`repay` call, maximizing dilution of the victim's accrued index.

### Impact Explanation
Unclaimed yield is effectively stolen/destroyed: rewards that should have accrued to DebtToken holders instead remain undistributed in the `RewardsDistributor` (or are re-proportioned to whoever claims later). The loss is proportional to `interestRate * elapsed`, i.e., continuous and compounding on every pool where a DebtToken has a nonzero `tokenSpeed` — this is a live configuration (mainnet deployments distribute rewards on debt tokens per the test suite and `updateTokenSpeed` paths). `_transferRewardIfEnoughTokens` (line 248) happily under-pays since `tokensAccruedOf` itself is understated.

### Likelihood Explanation
High/deterministic for the passive case (any `issue`, `repay`, `repayAll`, `mint`, `flashIssue`, `updateInterestRate`, or direct call triggers `accrueInterest`, and `interestRate > 0` is the normal state), and actively triggerable by an unprivileged EOA since `accrueInterest()` is `public` with no modifier. No privileged role, pause flag, or supply cap prevents it — `onlyIfDebtTokenIsActive` doesn't apply to `accrueInterest` itself.

### Recommendation
Apply the index checkpoint before mutating `totalSupply_` in `accrueInterest`: either wrap the `totalSupply_ += _interestAmountAccrued` write with an `updateRewardsBeforeMintOrBurn`-equivalent that calls `IRewardsDistributor(d).updateBeforeMintOrBurn(this, address(0))`-style index update for each rewards distributor (a pure index refresh is sufficient — the dilution stems from stale supply), or have `RewardsDistributor` accrue against a time-weighted/averaged supply. Additionally, consider checkpointing the index inside `updateBeforeMintOrBurn` callers before interest is accrued in `issue`/`repay`/`mint`/`flashIssue` (i.e., reorder `accrueInterest()` to run after the rewards update).

### Proof of Concept
Hardhat-style sketch (mirroring `test/RewardDistributor.test.ts` fixtures, which already use `updateTokenSpeed` on a DebtToken):

```ts
// Setup: rewardDistributor.updateTokenSpeed(debtToken, speed = 1e18)
// debtToken has interestRate > 0 and existing debt supply S.

// 1. User A mints debt (index checkpointed via _mint's modifier).
await debtToken.issue(amount, alice.address); // tokenStates.index = I0

// 2. Time passes; interest accrues internally.
await time.increase(30 * 24 * 3600);

// 3. Trigger the bug: accrueInterest grows totalSupply_ WITHOUT index update.
//    Anyone can call this — no privilege required.
await debtToken.accrueInterest(); // totalSupply_ = S * (1 + r*t)

// 4. Now checkpoint the index (any mint/burn/claim path).
await rewardDistributor.updateBeforeMintOrBurn(debtToken.address, alice.address);

const { index } = await rewardDistributor.tokenStates(debtToken.address);
// BUG: index == I0 + (30d * speed) / (S * (1 + r*t))
// EXPECTED (approx): index == I0 + integral over growing supply,
//      or at minimum index computed against the supply that existed
//      during the accrual window — strictly larger than the buggy value.

// Victim's accrued rewards are permanently lower:
const accrued = await rewardDistributor.tokensAccruedOf(alice.address);
// accrued < 30d-worth of rewards proportional to true average balance
```

The delta between the buggy and expected index is the stolen/diluted yield, recoverable only by whoever happens to hold the inflated balance afterward — a direct loss of unclaimed rewards for all other holders.