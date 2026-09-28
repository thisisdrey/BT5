### Title
Frontrunning `Pool.liquidate` with a dust `repay` forces `AmountGreaterThanMaxLiquidable` revert — ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` re-computes the liquidation bound against the live on-chain `DebtToken` balance, while the liquidator chooses `amountToRepay_` off-chain against the balance observed at transaction-construction time. An unprivileged attacker can frontrun the liquidation with a dust-sized `DebtToken.repay` on behalf of the victim, shrinking `balanceOf(account_)` just enough that `amountToRepay_.wadDiv(balance) > maxLiquidable` and the whole liquidation reverts — the same bug class as Sturdy's `manualAllocation` (off-chain parameters computed against state `s`, frontrun to `s'` produces a revert inside a permissionless entry point).

### Finding Description
`Pool.liquidate` is permissionless and executes the following sequence:

- `accrueInterest()` and health check on the target account
- `_debtTokenBalance = _debtToken.balanceOf(account_)`
- `if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable()` (contracts/Pool.sol:567-569)

`amountToRepay_` is a caller-supplied argument fixed at signing time. `DebtToken.repay(onBehalfOf_, amount_)` is a public function that lets any holder of the synthetic asset burn it to reduce another account's debt balance (the repay path is exercised for arbitrary `onBehalfOf` in test/DebtToken.test.ts:381-440). There is no slippage/tolerance path: the bound is enforced with a hard revert rather than clamping the amount.

Attack trace:

1. Victim `alice` is unhealthy; `debtBalance = D`, `maxLiquidable = 50%`.
2. Liquidator submits `liquidate(msToken, alice, D/2, depositToken)` — maximally sized liquidation.
3. Attacker frontruns with `debtToken.repay(alice, 1)` (repays 1 wei of alice's debt, costing the attacker ~1 wei of the synthetic asset plus gas).
4. `balanceOf(alice)` is now `D - 1`; `wadDiv(D/2, D-1) > 0.5e18` → `AmountGreaterThanMaxLiquidable` revert.
5. The liquidator must re-quote and resubmit; the attacker can repeat each block for as long as it is profitable to keep the position un-liquidated (e.g., attacker is a competing liquidator waiting for a better price, or aligned with the victim), or keep repaying until the position turns healthy so the tx reverts with `PositionIsHealthy` (contracts/Pool.sol:559-563).

The same manipulation also composes with `debtFloorInUsd`: a dust repay can push the residual debt below the floor so the tx reverts with `RemainingDebtIsLowerThanTheFloor` (contracts/Pool.sol:571-579).

### Impact Explanation
Liveness violation / temporary freezing of funds: liquidations of borderline positions can be delayed or repeatedly reverted at dust cost, keeping an unhealthy position alive and collateral locked while it drifts toward bad debt. This matches the original report's class — a state snapshot assumed at signing time is invalidated by an unprivileged frontrunner who has financial incentive to force the revert.

### Likelihood Explanation
Medium-low. The attack is cheap (dust repay + gas) and fully permissionless, but the liquidator can resubmit with a slightly smaller `amountToRepay_` or use `maxLiquidable`-exclusive sizing, so sustained blocking requires the attacker to keep racing. A guaranteed block requires the more expensive variant (repay enough to flip health, or a debt-floor sandwich). No governor/keeper/oracle role is needed.

### Recommendation
Clamp instead of reverting, mirroring the upstream fix (`continue` on the unreachable state):

```solidity
uint256 _maxLiquidableAmount = _debtTokenBalance.wadMul(maxLiquidable);
uint256 _amountToRepay = amountToRepay_ > _maxLiquidableAmount
    ? _maxLiquidableAmount
    : amountToRepay_;
```

Then proceed with `_amountToRepay`. Similarly, treat the debt-floor check as a floor on the repayable amount (repay down to the floor, or require full repayment) rather than reverting. Alternatively accept a `minAmountToRepay_` tolerance parameter so liquidators express acceptable slippage.

### Proof of Concept
Hardhat-style reproduction (fork or unit test):

```ts
// alice has unhealthy position, debt D on msUSDDebt, maxLiquidable = 50%
const debt = await msUSDDebt.balanceOf(alice.address);
const amountToRepay = debt.div(2); // exactly at maxLiquidable

// attacker holds >= 1 wei of msUSD (minted via own position or bought)
await msUSDDebt.connect(attacker).repay(alice.address, 1); // frontrun

await expect(
  pool.connect(liquidator).liquidate(msUSD.address, alice.address, amountToRepay, msdMET.address)
).to.revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');
```

This mirrors the existing debt-floor and max-liquidable revert tests in test/Pool.test.ts:408-451, which confirm both revert paths are live in the deployed configuration.

Confidence note: Metronome has no permissionless allocator/keeper loop analogous to `DebtManager.manualAllocation` (Treasury loops are `onlyPool`/`onlyGovernor`, `syncTokenSpeed` is keeper-gated). `Pool.liquidate` is the strongest reachable surface for this bug class — same root cause (hard revert on a bound evaluated against mutable state), same attacker model (unprivileged frontrunner), but lower impact than the Sturdy original since retry-with-smaller-amount is a cheap workaround.