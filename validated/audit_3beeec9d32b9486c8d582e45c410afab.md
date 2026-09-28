### Title
Debt floor combined with the `maxLiquidable` cap makes underwater positions permanently unliquidatable - ([File: contracts/Pool.sol])

### Summary
`Pool.liquidate` enforces two independent bounds on `amountToRepay_`: it may not exceed `maxLiquidable` (50% by default) of the account's debt, and it may not leave a non-zero remainder below `debtFloorInUsd`. For any account whose debt is in the range `(debtFloorInUsd, 2 * debtFloorInUsd]`, every admissible repayment amount violates one of the two checks, so `liquidate` reverts unconditionally. An unprivileged attacker can deliberately open such a position (`DebtToken._mint` only requires the new debt to be **at or above** the floor, not above `2 * floor`) and let it go unhealthy, permanently blocking liquidation of their collateral.

### Finding Description
In `Pool.liquidate`, the repayment amount is bounded above by the liquidation cap:

```solidity
// contracts/Pool.sol:565-569
uint256 _debtTokenBalance = _debtToken.balanceOf(account_);
if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
```

and bounded below by the residual debt-floor check:

```solidity
// contracts/Pool.sol:571-578
uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
    address(syntheticToken_),
    _debtTokenBalance - amountToRepay_
);
if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
    revert RemainingDebtIsLowerThanTheFloor();
}
```

With the default `maxLiquidable = 0.5e18` (set in `initialize`), the maximum repayable amount is `D / 2`, leaving a remainder of at least `D / 2`. For any debt `D` satisfying `debtFloorInUsd < D < 2 * debtFloorInUsd`:

- full repayment (`amountToRepay_ = D`) exceeds `D * maxLiquidable` → `AmountGreaterThanMaxLiquidable`;
- any repayment `≤ D/2` leaves a remainder in `(0, debtFloorInUsd)` → `RemainingDebtIsLowerThanTheFloor`.

There is no valid `amountToRepay_`. The same floor logic in `DebtToken.repay` (contracts/DebtToken.sol:442-451) only blocks *partial* repayments, and `repayAll` is a donation — a third party would have to gift the borrower synths to clear the debt, which no liquidator will do. Meanwhile, `DebtToken._mint` only enforces `balance + amount ≥ debtFloorInUsd` (contracts/DebtToken.sol:583-588), so an attacker can deliberately issue debt in the dead zone in a single `issue` call.

### Impact Explanation
Liveness / solvency break on the liquidation path. Once the attacker's position becomes unhealthy (normal market movement suffices), no liquidator can seize their collateral: `liquidate` always reverts, and `DepositToken.seize` is only reachable via `onlyIfCanSeize` → `Pool.liquidate`. The attacker keeps the minted synthetic tokens (value already extracted) while their locked collateral — worth less than the debt — can never be seized. The protocol is forced to either absorb the bad debt or have governance intervene (raise `debtFloorInUsd`/`maxLiquidable` or `shutdown`, which halts withdraws for everyone). Each such position is a permanent, direct protocol insolvency proportional to the undercollateralized amount.

### Likelihood Explanation
Fully reachable by an unprivileged EOA using only public entry points: `DepositToken.deposit` + `DebtToken.issue` with an amount sized just above `debtFloorInUsd` (the attacker must satisfy `amount * price ≥ floor`, and choose `amount < 2 * floor / price`). Then wait for the position to drift below the collateral factor via normal price movement — no oracle manipulation, privileged role, governance change, or external contract is required. The attacker's cost is only the (locked) collateral; even if the position is only marginally unhealthy, the protocol can never recover it, and the attacker may additionally recover value if the position later becomes healthy again and they repay and withdraw. The only prerequisites are a non-zero `debtFloorInUsd` (a deployed-config parameter documented in `docs/emergency-flags.md` and present on live deployments) and `maxLiquidable < 1e18` (default 50%).

### Recommendation
Reconcile the two bounds so a full liquidation is always possible for small debts. Concretely, in `Pool.liquidate` either:
- allow `amountToRepay_ == _debtTokenBalance` (full close) regardless of `maxLiquidable`, or
- skip the `RemainingDebtIsLowerThanTheFloor` check when `amountToRepay_` equals the maximum permitted by `maxLiquidable` and instead force the remainder to zero by allowing full repayment, or
- enforce at mint time that new debt is either zero or `≥ 2 * debtFloorInUsd` relative to `maxLiquidable` (fragile — depends on both params staying in sync).

The cleanest fix is: if `amountToRepay_ >= _debtTokenBalance.wadMul(maxLiquidable)` would leave a sub-floor remainder, permit repayment of the full balance.

### Proof of Concept
Foundry fork test outline (against deployed `Pool`/`DebtToken` proxies):

```solidity
function test_UnliquidatableDebtFloor() public {
    // setup: pool with debtFloorInUsd = F > 0, maxLiquidable = 0.5e18 (default)
    // attacker deposits collateral and issues debt D with F < debtUsd(D) < 2F
    depositToken.deposit(collateralAmount, attacker);
    uint256 issueAmt = (F + 1).wadDiv(synthPrice); // debt just above floor
    debtToken.issue(issueAmt, attacker); // succeeds: _mint only requires >= floor

    // oracle moves so position becomes unhealthy (mock MasterOracle price drop)
    masterOracle.updatePrice(address(collateral), lowerPrice);
    (bool healthy,,,,) = pool.debtPositionOf(attacker);
    assertFalse(healthy);

    uint256 debt = debtToken.balanceOf(attacker); // still in (F, 2F) in USD terms
    uint256 maxRepay = debt.wadMul(pool.maxLiquidable()); // = debt / 2
    // remainder in USD = (debt - maxRepay) * price ∈ (0, F) -> always reverts

    vm.expectRevert(RemainingDebtIsLowerThanTheFloor.selector);
    pool.liquidate(synth, attacker, maxRepay, depositToken);

    // trying more than maxRepay also reverts
    vm.expectRevert(AmountGreaterThanMaxLiquidable.selector);
    pool.liquidate(synth, attacker, maxRepay + 1, depositToken);

    // position is permanently unliquidatable while debt ∈ (floor, 2*floor]
}
```

Note: I verified the revert conditions and entry points directly in `contracts/Pool.sol` and `contracts/DebtToken.sol`; I did not inspect the deployed on-chain `debtFloorInUsd` values, so the finding assumes `debtFloorInUsd > 0` on the target deployment (the check is a no-op when it is 0).