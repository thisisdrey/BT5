### Title
`Pool.liquidate` seizes smart-farming–locked `DepositToken` collateral, orphaning SmartFarmingManager's locked-balance accounting (use-after-free of collateral already committed to a leveraged position) - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DepositToken.seize()` performs a raw `_transfer` with no `_revertIfLocked` check, so `Pool.liquidate` can seize deposit tokens that are locked as backing for a SmartFarmingManager leveraged position. The tokens are transferred to the liquidator while `SmartFarmingManager` still counts them as the account's locked collateral — the same collateral is effectively used twice: once to seize, once still "owed" to the farming position. This is the direct analog of a use-after-free: collateral released by liquidation continues to be referenced by the smart-farming accounting, corrupting both solvency tracking and future withdrawals.

### Finding Description
`Pool.liquidate` validates only `_totalSeized > depositToken_.balanceOf(account_)` (total balance, locked included) and then calls `depositToken_.seize(...)` [1](#0-0) . `seize` is gated by `onlyIfCanSeize` (caller must be the pool) and executes `_transfer` directly [2](#0-1) . `_transfer` has no `_revertIfLocked` guard — unlike `transfer`, `transferFrom`, `withdraw`, and `withdrawFrom`, which all enforce `unlockedBalanceOf` [3](#0-2) [4](#0-3) .

Locked balance is tracked implicitly: `lockedBalanceOf = balanceOf[account] - unlockedBalanceOf(account)`, where `unlockedBalanceOf` derives from the account's live debt position [5](#0-4) [6](#0-5) . After liquidation reduces `balanceOf`, any subsequent SmartFarmingManager operation that relies on the locked collateral — `flashWithdraw`/`withdrawFrom` from the farming position — will attempt to `_burn`/`_withdraw` tokens the account no longer holds and revert with `BurnAmountExceedsBalance`/`NotEnoughFreeBalance` [7](#0-6) .

Attack path (unprivileged liquidator):
1. Victim deposits collateral and opens a leveraged position via `SmartFarmingManager.leverage`; the msdTOKEN balance is locked (unwithdrawable) while the flash-minted debt keeps the position near the liquidation threshold.
2. Attacker buys the pool's synthetic token on the open market (or via `Pool.swap`) and calls `Pool.liquidate(account_, syntheticToken_, amountToRepay_, depositToken_)` once the position dips below healthy.
3. `liquidate` repays part of the debt — which *raises* `unlockedBalanceOf` is not the point; the seize itself draws down `balanceOf` including locked tokens — and transfers `_toLiquidator + _fee` out of the account.
4. The leveraged position's remaining debt may still exist (only `maxLiquidable` portion is repaid), but the collateral backing it was already seized. Any later unwind through SmartFarmingManager (`flashRepay`/unlock flow) reverts because the tokens are gone — the "freed" collateral is still referenced.

### Impact Explanation
- Permanent freezing of funds: the victim's leveraged position can never be unwound through `SmartFarmingManager` because the underlying msdTOKEN balance was seized while still recorded as locked backing; `withdrawFrom`/`flashWithdraw` revert on the missing balance.
- Protocol accounting inconsistency / bad debt: the residual debt may remain backed by collateral that no longer exists in the account, while `debtPositionOf` still reports remaining `depositTokensOfAccount`/`debtTokensOfAccount` entries only if balances are non-zero — the position can end in a state where collateral was extracted but debt persists at below-floor dust levels that `RemainingDebtIsLowerThanTheFloor` blocks from further liquidation [8](#0-7) .

### Likelihood Explanation
Any liquidator holding the synthetic token can trigger this; liquidation is permissionless and `seize` skips the lock check by design (the pool is trusted to seize). The condition — a leveraged position that becomes liquidatable — is routine. No privileged role, oracle manipulation, or governance action is required. The main mitigating factor is that seizing locked tokens is arguably intentional (liquidation must be able to take all collateral), but the accounting inconsistency with SmartFarmingManager's locked-position bookkeeping is not handled: there is no path that force-unlocks or settles the farming position when its collateral is seized, so funds remain permanently stuck.

### Recommendation
- When seizing tokens that back a SmartFarmingManager position, route the liquidation through SmartFarmingManager (or notify it) so the leveraged position is atomically unwound/settled rather than leaving dangling locked accounting.
- Alternatively, cap `quoteLiquidateOut`/seizure in `Pool.liquidate` to `unlockedBalanceOf(account_)` plus a properly-settled locked portion, or introduce a `forceUnlock` hook the pool invokes on `seize`.
- Add a regression test: open leveraged position → liquidate → assert SmartFarmingManager unwind path still works or is atomically closed.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch
// 1. victim deposits WETH -> msdWETH; calls sfm.leverage(...) -> locked msdWETH + msUSD debt
// 2. warp until position unhealthy (or same-tx AMM price move on oracle source)
// 3. attacker: msUSD.approve / pool.swap to acquire msUSD, then
pool.liquidate(victim, msUSD, maxLiquidableAmount, msdWETH);
// 4. seize transferred locked tokens; msdWETH.balanceOf(victim) < lockedBalance previously recorded
// 5. victim or keeper calls sfm unwind / msdWETH.withdrawFrom(victim, locked, ...)
//    -> reverts NotEnoughFreeBalance / BurnAmountExceedsBalance
//    leveraged collateral permanently frozen; residual dust debt unliquidatable via debtFloorInUsd check
```

### Citations

**File:** contracts/Pool.sol (L571-579)
```text
        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }
```

**File:** contracts/Pool.sol (L583-593)
```text
        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/DepositToken.sol (L266-270)
```text
    function lockedBalanceOf(address account_) external view override returns (uint256 _lockedBalance) {
        unchecked {
            return balanceOf[account_] - unlockedBalanceOf(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L406-411)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
```

**File:** contracts/DepositToken.sol (L447-448)
```text
        uint256 _balanceBefore = balanceOf[_account];
        if (_balanceBefore < _amount) revert BurnAmountExceedsBalance();
```
