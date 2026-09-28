### Title
Uninitialized `accountIndexOf` treated as 0 instead of `INITIAL_INDEX` lets an attacker instantly accrue rewards equal to their full token balance - (File: contracts/RewardsDistributor.sol)

### Summary
The CVE class here is "improperly initialized state → unexpected zero value corrupts computation." The Metronome analog is in `RewardsDistributor._calculateTokenDelta`: a fresh account's `accountIndexOf[token][account]` is `0`, and the code only corrects it to `INITIAL_INDEX` when the global token index has already grown past `INITIAL_INDEX`. When the index is still exactly `INITIAL_INDEX` (i.e., right after a reward token is registered but before any index update), the delta is computed as `INITIAL_INDEX - 0`, crediting the account `balance * 1.0` reward tokens instantly.

### Finding Description
In `contracts/RewardsDistributor.sol`, `_calculateTokenDelta` handles the "account has no recorded index" case only when `_tokenIndex > INITIAL_INDEX`: [1](#0-0) 

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a governor sets a nonzero speed for a new token, `_updateTokenSpeed` registers it with `index = INITIAL_INDEX` and pushes it to `tokens`: [2](#0-1) 

Until `block.timestamp` advances past that stored timestamp (so `_calculateTokenIndex` produces `_newIndex > 0`), `tokenStates[token].index == INITIAL_INDEX`. Any call to `updateBeforeMintOrBurn` / `updateBeforeTransfer` / `claimRewards` in this window hits `_updateTokensAccruedOf` → `_calculateTokenDelta` with `_tokenIndex == INITIAL_INDEX`. Since `INITIAL_INDEX > INITIAL_INDEX` is false, `_accountIndex` stays `0`, `_deltaIndex = 1e18`, and `_tokensDelta = balance.wadMul(1e18) = balance`. The account's entire DepositToken/DebtToken balance is credited as claimable `rewardToken`, and `accountIndexOf` is then stored as `INITIAL_INDEX`, so it can be done once per account per token-registration event.

The attacker path is fully unprivileged:
1. Monitor mempool for `updateTokenSpeed(token_, newSpeed_ > 0)` (or `syncTokenSpeed` by the keeper on a token whose index is 0).
2. In the same block, deposit a large amount of underlying via `DepositToken.deposit` (or use a flash loan / existing position) to obtain a large `token_.balanceOf(attacker)`.
3. Call `RewardsDistributor.updateBeforeMintOrBurn(token_, attacker)` — explicitly permissionless ("This function also may be called by anyone to update stored indexes", line 173) — while `tokenStates[token_].index == INITIAL_INDEX` and `timestamp == block.timestamp`.
4. Call `claimRewards(attacker, [token_])`; `_transferRewardIfEnoughTokens` pays out `tokensAccruedOf[attacker]` up to the distributor's whole `rewardToken` balance. [3](#0-2) [4](#0-3) 

### Impact Explanation
Theft of unclaimed yield / direct theft of protocol reward funds. An attacker holding (or flash-borrowing, where the deposit→accrue→claim→withdraw path can be atomically arranged via `Operator.execute`/gateways) a large DepositToken or DebtToken balance receives `rewardToken` equal to that balance with zero reward accrual time. Legitimate users' accrued-but-unclaimed rewards are drained, since `_transferRewardIfEnoughTokens` pays whoever claims first against the distributor's full `rewardToken` balance. For a Whale-scale deposit (e.g., 1M USDC → ~1M msdUSDC), this mint-credits ~1M units of reward token in one call.

### Likelihood Explanation
Requires a new token speed activation (or re-activation where `tokenStates[token].index` was set in the same block) — a governor/keeper transaction — which the attacker front-runs in the same block. This is an ordinary, low-cost sequencing requirement for an unprivileged EOA; no privileged role, malicious oracle, or third-party failure is needed. The window closes once any index update occurs at a later timestamp, but the attacker controls the entire exploit atomically in the same block as the registration tx.

### Recommendation
Initialize `_accountIndex` to `INITIAL_INDEX` whenever it is `0`, not only when `_tokenIndex > INITIAL_INDEX`:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;
    // or simply: _accountIndex = INITIAL_INDEX;
}
```

Equivalently, treat `accountIndexOf == 0` as "starts at `INITIAL_INDEX`" unconditionally, so `_deltaIndex` is `0` for any fresh account while the global index has not grown. Add a test covering `updateBeforeMintOrBurn` in the same block as `updateTokenSpeed` for a brand-new account.

### Proof of Concept
Hardhat fork-style (same structure as `test/E2E.base.test.ts`):

```ts
it('over-accrues rewards for fresh account in the same block as speed set', async () => {
  // governor registers rewards on msdUSDC; attacker front-runs in same block
  // hardhat: send governor tx with automine off, then attacker txs, then mine
  await rewardsDistributor.connect(governor).updateTokenSpeed(msdUSDC.address, parseEther('1'));

  // same block / same timestamp: index == INITIAL_INDEX, timestamp == now
  await msdUSDC.connect(attacker).deposit(parseUnits('1000000', 6), attacker.address);

  // permissionless index/accrual update; no time has passed
  await rewardsDistributor.updateBeforeMintOrBurn(msdUSDC.address, attacker.address);

  const claimable = await rewardsDistributor.claimable(attacker.address);
  // BUG: claimable == attacker deposit-token balance, not ~0
  expect(claimable).to.eq(await msdUSDC.balanceOf(attacker.address));

  // fund distributor (as it is in production) and drain
  await rewardToken.mint(rewardsDistributor.address, claimable);
  await rewardsDistributor.claimRewards(attacker.address, [msdUSDC.address]);
  expect(await rewardToken.balanceOf(attacker.address)).to.eq(claimable);
});
```

Expected fixed behavior: `claimable` should be `0` for a brand-new account while the global index is still `INITIAL_INDEX`.

### Citations

**File:** contracts/RewardsDistributor.sol (L175-180)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L223-230)
```text
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L248-255)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
