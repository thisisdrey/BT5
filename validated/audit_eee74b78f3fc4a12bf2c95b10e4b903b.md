### Title
Underwater positions with residual debt below `debtFloorInUsd` can never be liquidated, permanently locking the borrower's remaining collateral - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` combines two checks that are individually sound but jointly create a dead state: a liquidation that would leave a nonzero residual debt smaller than `debtFloorInUsd` reverts with `RemainingDebtIsLowerThanTheFloor`, while a liquidation that repays all debt but would seize more deposit tokens than the account holds reverts with `AmountIsTooHigh`. For an undercollateralized account whose remaining debt is below the floor, every possible `amountToRepay_` value falls into one of these two reverts, so the position is permanently unliquidatable and the borrower's remaining collateral is permanently locked because `_revertIfLocked`/`unlockedBalanceOf` return zero while any debt exists. [1](#0-0) 

### Finding Description
`liquidate` enforces:

1. `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` → revert (`maxLiquidable` is typically < 1e18, e.g. 50%). [2](#0-1) 
2. If `debtFloorInUsd > 0`, repaying less than the full balance such that `0 < newDebtInUsd < debtFloorInUsd` reverts with `RemainingDebtIsLowerThanTheFloor`. Only repaying exactly the full debt avoids this branch. [3](#0-2) 
3. `_totalSeized > depositToken_.balanceOf(account_)` reverts with `AmountIsTooHigh`, where `_totalSeized` = repaid value + liquidator incentive + protocol fee, quoted via the oracle. [4](#0-3) 

For a position where `debtInUsd > depositInUsd * (1 + incentives)` (i.e. the collateral cannot even cover the seize for a full repayment), a full repayment reverts via check 3, and any partial repayment that leaves residual debt under `debtFloorInUsd` reverts via check 2. Once accrued interest or a small additional liquidation pushes the residual debt below `debtFloorInUsd`, no `amountToRepay_` exists that satisfies both checks simultaneously:

- repay `< balance` → residual debt `< debtFloorInUsd` → `RemainingDebtIsLowerThanTheFloor`
- repay `== balance` → `_totalSeized > balanceOf` → `AmountIsTooHigh`

There is no admin-free escape path: `DepositToken.seize` is `onlyIfCanSeize` (Pool only), and the borrower cannot `withdraw` or `transfer` collateral while `debtPositionOf` reports any debt, since `unlockedBalanceOf` returns 0 whenever `_debtInUsd > 0`. [5](#0-4) [6](#0-5) 

### Impact Explanation
Permanent freezing of user funds and stuck bad debt. The borrower's residual collateral can never be withdrawn (debt > 0 ⇒ `unlockedBalanceOf == 0`) and can never be seized by a liquidator (every repayment amount reverts). The protocol is also left with uncollectable dust debt. This is the same class as the referenced finding: a liquidation flow that reaches a terminal state where no actor can complete it. `debtFloorInUsd` is a deployed-config parameter (set nonzero on mainnet deployments) and the underwater state is reachable through ordinary price movement plus interest accrual — no oracle manipulation or privileged action is required.

### Likelihood Explanation
Moderate. It requires a position to go sufficiently underwater that full repayment's seize exceeds the deposit balance while residual debt is under the floor — plausible during sharp collateral drawdowns, exactly the scenario liquidations exist for. Any user can also deliberately engineer this on their own position (mint to the limit, let interest accrue / price drift), after which neither they nor any third party can ever recover the remaining collateral or clear the debt. Because `maxLiquidable` is applied via `wadMul` on the debt balance and `quoteLiquidateMax` returns the collateral-capped maximum, a liquidator cannot even partially reduce the position once the residual debt is dust-sized: the dust remainder is below the floor and the only floor-compliant repayment (full) exceeds seizable collateral.

### Recommendation
Apply the floor check only when the position is solvent enough to be partially liquidated, or compute the floor against the maximum actually repayable amount: e.g. skip `RemainingDebtIsLowerThanTheFloor` when `amountToRepay_ == quoteLiquidateMax(...)` / when the seize is capped by the account's balance, and cap `_totalSeized` at `depositToken_.balanceOf(account_)` for full repayments rather than reverting. Alternatively, treat residual debt `< debtFloorInUsd` as liquidatable-in-full with `totalSeized = min(quoted, balance)`.

### Proof of Concept
Hardhat-style sketch (repo's existing `Pool.test.ts` harness: `masterOracle.updatePrice`, `msdMET` deposit, `msEth` issue):

```ts
// 1. Alice deposits MET (msdMET) and mints msETH near max.
// 2. Crash MET price so debt >> collateral (deep underwater).
await masterOracle.updatePrice(met.address, toUSD('0.05'))

// 3. Partial liquidations (each capped by maxLiquidable / collateral)
//    reduce the debt until residual debt < debtFloorInUsd.
// 4. Now every liquidate call reverts:
//    - repay == balance       -> AmountIsTooHigh (seize > balanceOf)
//    - repay <  balance       -> RemainingDebtIsLowerThanTheFloor
const debt = await msEthDebtToken.balanceOf(alice.address)
await expect(
  pool.connect(liq).liquidate(msEth.address, alice.address, debt, msdMET.address)
).revertedWithCustomError(pool, 'AmountIsTooHigh')

await expect(
  pool.connect(liq).liquidate(msEth.address, alice.address, debt.div(2), msdMET.address)
).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')

// 5. Alice's remaining msdMET is locked forever:
expect(await msdMET.unlockedBalanceOf(alice.address)).eq(0)
await expect(
  msdMET.connect(alice).withdraw(alice.address, 1)
).reverted // LockedBalance / insufficient unlocked balance
```

The state persists across subsequent calls and new signatures/prices do not help unless the oracle price recovers enough to make a full-repay seize fit the balance — identical in spirit to "liquidation stuck, funds locked forever".

### Citations

**File:** contracts/Pool.sol (L565-585)
```text
        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }

        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L383-390)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }
```
