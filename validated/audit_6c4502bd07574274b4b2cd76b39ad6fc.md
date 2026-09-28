### Title
`RewardsDistributor` rewards are computed on `DebtToken.balanceOf` which grows with accrued interest without any `updateBeforeMintOrBurn` notification, letting early claimants over-draw reward tokens at the expense of other users - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` settles an account's accrued rewards as `token_.balanceOf(account_).wadMul(deltaIndex)` in `_calculateTokenDelta` [1](#0-0) . It relies on `DepositToken`/`DebtToken` calling `updateBeforeMintOrBurn`/`updateBeforeTransfer` before every balance change [2](#0-1) . However, `DebtToken` balances also change through interest accrual: `balanceOf`/`totalSupply` grow with `debtIndex` via `_calculateInterestAccrual`/`accrueInterest` without touching the rewards contract [3](#0-2) . This mirrors the LiquidityGauge bug: a balance-changing contract (VotingEscrow → DebtToken) updates balances without notifying the reward-accruing contract (gauge → RewardsDistributor), so accrual is computed on a balance the distributor never observed.

### Finding Description
- `DebtToken.balanceOf(account)` returns `principalOf[account]` scaled by the current `debtIndex` (which grows every second at `interestRatePerSecond`), and `totalSupply()` returns `totalSupply_ + _interestAmountAccrued` [4](#0-3) .
- `accrueInterest()`/`_calculateInterestAccrual()` update `debtIndex` and `lastTimestampAccrued` with no call to `pool.getRewardsDistributors()` — no `updateBeforeMintOrBurn` hook exists on the interest path [5](#0-4) .
- When rewards are finally settled — via the permissionless `updateBeforeMintOrBurn`, `claimRewards`, or a mint/burn hook — `_calculateTokenDelta` multiplies the account's *current* (interest-inflated) `balanceOf` by the *entire* `deltaIndex` accumulated since `accountIndexOf` was last stored [6](#0-5) .
- The index itself is computed against `token_.totalSupply()` [7](#0-6) , which grows at the same `debtIndex` rate, so the aggregate per-account over-credit compounds: every holder's whole `deltaIndex` window is priced at their grown end-of-window balance, paying out more reward tokens than `speed * elapsed` allocated.
- `_transferRewardIfEnoughTokens` pays out `tokensAccruedOf[account]` as long as the distributor's `rewardToken` balance covers it, and silently pays nothing otherwise [8](#0-7) . The first accounts to settle drain the inflated amounts; later claimants' accrued rewards are unpayable.

### Impact Explanation
Theft of unclaimed yield: a borrower on a reward-enabled `DebtToken` (borrow incentives are a supported config — `tokenSpeeds` may target any deposit or debt token) accrues rewards on a balance that includes interest never seen by the distributor. They collect strictly more than their pro-rata share of `speed * elapsed`; because the distributor's reward token balance is finite, honest users' `tokensAccruedOf` become permanently unclaimable (`_transferRewardIfEnoughTokens` just skips the transfer). The invariant "rewards distributed per second = `tokenSpeeds[token]`" is broken.

### Likelihood Explanation
Requires (a) a `RewardsDistributor` instance with non-zero `tokenSpeeds[debtToken]` (deployed config supports this; tests exercise debt-token rewards), and (b) `interestRate > 0` on that debt token — the normal case for msUSD/msETH debt. The attack is fully unprivileged: `issue()` is public with sufficient collateral, and `updateBeforeMintOrBurn`/`claimRewards` are permissionless. The longer interest accrues between an account's index updates, the larger the over-credit, so an attacker can maximize the drain by issuing early, never interacting again, and settling right before honest users. No pause, health, supply-cap, or reentrancy check intervenes: `_burn`'s `updateRewardsBeforeMintOrBurn` hook settles at the already-inflated `balanceOf` [9](#0-8) .

### Recommendation
Use a balance measure that excludes accrued-but-unrealized interest for reward accrual on debt tokens (e.g. settle on `principalOf`-equivalent), or make `accrueInterest()` notify registered rewards distributors before `debtIndex` changes so deltas are priced on the balance actually held during each window. Alternatively, document that rewards on debt tokens must use `speed = 0` and enforce it in `updateTokenSpeed`.

### Proof of Concept
Hardhat (repo's existing harness, see `test/RewardDistributor.test.ts` patterns):

```ts
// Setup: real Pool + DebtToken(msUSD) with interestRate > 0,
// RewardsDistributor with tokenSpeeds[debtToken] = 1 VSP/sec,
// distributor funded with N reward tokens.

// 1. Alice deposits collateral and calls debtToken.issue(D, alice) at t0.
//    Bob does the same at t0 with the same D.
//    _mint -> updateRewardsBeforeMintOrBurn stores accountIndexOf = I0 for both.
// 2. Advance time to t1 without any mint/burn: accrueInterest() grows debtIndex
//    so balanceOf(alice) = balanceOf(bob) = D * k,  k = debtIndex(t1)/debtIndex(t0) > 1.
// 3. Alice calls rewardsDistributor.updateBeforeMintOrBurn(debtToken, alice)
//    then claimRewards(alice):
//      deltaIndex = I1 - I0  (covers the whole [t0,t1] window)
//      tokensAccruedOf[alice] = balanceOf(alice)=D*k  *  deltaIndex
//    i.e. Alice is paid as if she held D*k for the entire window,
//    although she held only D at t0..t1 (growing continuously).
// 4. Expected (intended) accrual for the window is speed*dt/totalSupply-weighted
//    principal; received amount is inflated by factor k for the early part.
// 5. Sum over all debt-token holders of over-credit exceeds the rewardToken
//    balance => later claims (e.g. Bob's claimRewards) hit
//    `amount_ > _balance` in _transferRewardIfEnoughTokens and receive 0.
// Assert: aliceReward * numberOfSymmetricHolders > speed * (t1-t0)
//         and bobReward == 0 while tokensAccruedOf[bob] > 0.
```

### Citations

**File:** contracts/RewardsDistributor.sol (L203-207)
```text
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
```

**File:** contracts/RewardsDistributor.sol (L229-230)
```text
        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L248-256)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L261-265)
```text
    function _updateTokensAccruedOf(IERC20 token_, address account_) private {
        (uint256 _tokenIndex, uint256 _tokensDelta) = _calculateTokenDelta(tokenStates[token_], token_, account_);
        accountIndexOf[token_][account_] = _tokenIndex;
        tokensAccruedOf[account_] = tokensAccruedOf[account_] + _tokensDelta;
        emit TokensAccruedUpdated(token_, account_, _tokensDelta, _tokenIndex);
```

**File:** contracts/DepositToken.sol (L124-131)
```text
    modifier updateRewardsBeforeMintOrBurn(address account_) {
        address[] memory _rewardsDistributors = pool.getRewardsDistributors();
        uint256 _length = _rewardsDistributors.length;
        for (uint256 i; i < _length; ++i) {
            IRewardsDistributor(_rewardsDistributors[i]).updateBeforeMintOrBurn(this, account_);
        }
        _;
    }
```

**File:** contracts/DebtToken.sol (L500-503)
```text
    function totalSupply() external view override returns (uint256) {
        (uint256 _interestAmountAccrued, , ) = _calculateInterestAccrual();
        return totalSupply_ + _interestAmountAccrued;
    }
```

**File:** contracts/DebtToken.sol (L525-535)
```text
    function _burn(address account_, uint256 amount_) private updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert BurnFromNullAddress();

        uint256 _accountBalance = balanceOf(account_);
        if (_accountBalance < amount_) revert BurnAmountExceedsBalance();

        unchecked {
            principalOf[account_] = _accountBalance - amount_;
            debtIndexOf[account_] = debtIndex;
            totalSupply_ -= amount_;
        }
```

**File:** contracts/DebtToken.sol (L551-566)
```text
    function _calculateInterestAccrual()
        private
        view
        returns (uint256 _interestAmountAccrued, uint256 _debtIndex, uint256 _lastTimestampAccrued)
    {
        _lastTimestampAccrued = lastTimestampAccrued;
        _debtIndex = debtIndex;

        if (block.timestamp > _lastTimestampAccrued) {
            uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
            if (_interestRateToAccrue > 0) {
                _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
                _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
            }
        }
    }
```
