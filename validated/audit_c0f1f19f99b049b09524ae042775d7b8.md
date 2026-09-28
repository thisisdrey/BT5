### Title
Uninitialized `accountIndexOf` credits users a full-index reward equal to their entire token balance - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta()` only normalizes a zero `accountIndexOf` when the global index is strictly greater than `INITIAL_INDEX`. When a user holds a `DepositToken`/`DebtToken` balance while `tokenStates[token_].index == INITIAL_INDEX` and their `accountIndexOf` is `0`, the index delta computes as `1e18 - 0 = 1e18`, crediting `tokensAccruedOf` with `balanceOf(account) * 1` — i.e. an amount of reward token equal to their full position balance. This is the on-chain analog of the duplicated-ownership/double-drop class: a phantom unit of ownership (a whole reward epoch) is materialized for an account that earned nothing.

### Finding Description
`claimRewards` and `updateBeforeMintOrBurn`/`updateBeforeTransfer` all funnel into `_updateTokensAccruedOf`, which calls `_calculateTokenDelta`: [1](#0-0) 

The guard `if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX)` fails when `_tokenIndex == INITIAL_INDEX` exactly. That state occurs when a token was registered by `_updateTokenSpeed` (`tokenStates[token_] = TokenState({index: INITIAL_INDEX, ...})` at line 298) but no time has elapsed to grow the index, i.e. any claim in the same block (or before index growth) after the token is added. Any account whose `accountIndexOf[token_][account] == 0` — every account that deposited/minted before reward speed was set, since `updateBeforeMintOrBurn` skips updating while `index == 0` (line 176) — then receives `_tokensDelta = balanceOf(account).wadMul(1e18) = balanceOf(account)` (lines 229–230), accumulated into `tokensAccruedOf` and paid out in `rewardToken` by `_transferRewardIfEnoughTokens` (lines 248–255). `claimRewards` is permissionless — anyone can trigger it for any account (`claimRewards(account_, tokens_)`, lines 141–168).

### Impact Explanation
Every user holding a tracked token at the moment its reward speed is first enabled receives reward tokens equal to their entire deposit/debt-token balance — a reward amount orders of magnitude larger than legitimate accrual, effectively "duplicating" their balance as claimable yield. The first claimants drain the distributor's `rewardToken` reserves (`safeTransfer` up to contract balance), stealing the protocol's reward budget and the unclaimed yield of all other users. Broken invariant: reward accrual conservation.

### Likelihood Explanation
Triggering requires the token to be registered at `INITIAL_INDEX` while users already hold balances with `accountIndexOf == 0` — the standard case for enabling incentives on an existing deposit token. Enabling a reward speed is routine protocol operation (the bug does not depend on any malicious privileged action; it fires deterministically once the configuration exists). The attacker merely needs to be a depositor (or to frontrun the registration block with a large deposit) and call `claimRewards`. Withdrawals, transfers, mint/burn — any `updateBeforeMintOrBurn` in that window also writes the phantom accrual. Impact is capped by the distributor's rewardToken balance but is deterministic and permissionless once the precondition holds.

### Recommendation
In `_calculateTokenDelta`, treat an uninitialized account index as "accrued from current index", not zero: when `_accountIndex == 0`, set `_accountIndex = _tokenIndex` (or at minimum `INITIAL_INDEX` whenever `_tokenIndex >= INITIAL_INDEX`), so `_deltaIndex` is `0` rather than a full index. Alternatively, eagerly initialize `accountIndexOf` to `INITIAL_INDEX` on first interaction with a tracked token.

### Proof of Concept
Hardhat fork sketch:

```ts
// Setup: pool, msdMET DepositToken, RewardsDistributor RD, rewardToken funded to RD.
// 1. Governor registers msdMET for rewards (speed > 0) -> tokenStates[msdMET].index = INITIAL_INDEX (1e18).
// 2. Alice already holds, or deposits in the same block, `X` msdMET with accountIndexOf[msdMET][alice] == 0.
//    (All pre-existing depositors satisfy this since updateBeforeMintOrBurn skipped while index == 0.)
// 3. Anyone calls `rd.claimRewards(alice.address)` in the same block (index still == INITIAL_INDEX).
// 4. _calculateTokenDelta: _accountIndex = 0, _tokenIndex = 1e18 (not > INITIAL_INDEX -> no fallback).
//    _deltaIndex = 1e18; _tokensDelta = X.wadMul(1e18) = X.
// 5. tokensAccruedOf[alice] = X; _transferRewardIfEnoughTokens transfers min(X, rdBalance) of rewardToken to alice.
// Assert: rewardToken.balanceOf(alice) == min(X, rdRewardBalance) instead of ~0.
```

Note: a Foundry invariant run that randomizes the order of "enable speed" vs "first claim" would reproduce this deterministically; the attacker's profit equals their whole token balance denominated in rewardToken, bounded by distributor reserves.

### Citations

**File:** contracts/RewardsDistributor.sol (L222-231)
```text
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
    }
```
