### Title
Uninitialized `accountIndexOf` (0 sentinel) treated as a real index in `RewardsDistributor._calculateTokenDelta` lets a holder claim reward tokens equal to their full balance — (File: contracts/RewardsDistributor.sol)

### Summary
In `RewardsDistributor`, `accountIndexOf[token][account] == 0` is the sentinel for "account index never initialized". The code is supposed to substitute `INITIAL_INDEX` (1e18) in that case, but it only does so when the current token index is strictly greater than `INITIAL_INDEX`. When `_tokenIndex == INITIAL_INDEX` — i.e., the token was registered but no index growth has been applied yet — the 0 sentinel is used verbatim, producing `_deltaIndex = 1e18` and crediting the account `balanceOf(account)` reward tokens instantly. This mirrors CVE-2017-14745's bug class: an error/uninitialized sentinel consumed as a numeric value.

### Finding Description
`RewardsDistributor._calculateTokenDelta` (contracts/RewardsDistributor.sol:217-231):

```solidity
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a token is first registered via `_updateTokenSpeed`, its state is set to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` (line 298). Before any successful index update, `_tokenIndex == INITIAL_INDEX`. The guard `_tokenIndex > INITIAL_INDEX` is false, so `_accountIndex` remains 0 and `_deltaIndex = 1e18 - 0 = 1e18`. Since `wadMul(x, 1e18) == x`, any account holding a balance of the DepositToken or DebtToken is credited `tokensAccruedOf += balance`.

Reachable from public entry points with no privileges:
- `claimRewards(address[] accounts, IERC20[] tokens)` (line 150, `nonReentrant` only) — anyone can pass their own address and the freshly registered token.
- `updateBeforeMintOrBurn` / `updateBeforeTransfer` (lines 175-192) — callable by anyone per the dev note.

The index stays at exactly `INITIAL_INDEX` whenever `_calculateTokenIndex` yields no growth: `_deltaTimestamps == 0` (same block as registration — back-runnable governor tx), `_speed == 0`, `totalSupply == 0`, or `_deltaTimestamps * _speed wadDiv totalSupply` rounding to 0 (small speed / huge supply). The attacker deposits collateral (minting `DepositToken`) before the token is registered, so `accountIndexOf` was never set for them.

### Impact Explanation
Theft of unclaimed yield / direct theft of reward funds. `_updateTokensAccruedOf` persists the inflated delta into `tokensAccruedOf`, and `claimRewards` then calls `_transferRewardIfEnoughTokens`, which transfers the distributor's entire `rewardToken` balance up to `tokensAccruedOf[account]`. An attacker with a large deposit position receives reward tokens equal to their raw share balance with zero accrual time, draining rewards legitimately owed to all users.

### Likelihood Explanation
Requirements: governor registers a reward speed for a token (normal operation), attacker holds the DepositToken/DebtToken beforehand, and attacker calls `claimRewards` before the index exceeds `INITIAL_INDEX` — trivially achievable in the same block as `updateTokenSpeed`, and potentially for many blocks afterward if `speed * elapsed / totalSupply` rounds to zero. No modifier, health check, pause flag, or reentrancy guard blocks it; `claimRewards` explicitly accepts arbitrary accounts.

### Recommendation
Replace the conditional fallback with an unconditional one: `if (_accountIndex == 0) { _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex; }`, or simply treat index 0 as "account is fully synced to current index" (`_accountIndex = _tokenIndex`) so no delta accrues for uninitialized accounts.

### Proof of Concept
Hardhat sketch (mainnet-fork or unit fixture with `RewardsDistributor` + a `DepositToken`):

```ts
// 1. attacker deposits underlying -> holds msdToken balance before token is registered
await underlying.mint(attacker.address, depositAmount);
await underlying.connect(attacker).approve(depositToken.address, depositAmount);
await depositToken.connect(attacker).deposit(depositAmount, attacker.address);

// 2. fund distributor
await rewardToken.mint(rewardsDistributor.address, rewardPool);

// 3. governor registers reward speed (fresh TokenState{index: INITIAL_INDEX})
await rewardsDistributor.connect(governor).updateTokenSpeed(depositToken.address, speed);

// 4. same block (deltaTimestamps == 0 -> index stays INITIAL_INDEX):
//    accountIndexOf == 0 is used verbatim -> deltaIndex == 1e18 -> delta == balance
await rewardsDistributor.connect(attacker).claimRewards(attacker.address, [depositToken.address]);

// attacker received rewardToken equal to their msdToken balance with zero accrual time
expect(await rewardToken.balanceOf(attacker.address)).to.eq(depositAmount);
```