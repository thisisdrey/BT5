### Title
Protocol liquidation fee rounds to zero on low-decimal collateral, allowing complete fee evasion via chunked liquidations - (File: contracts/Pool.sol)

### Summary
`Pool.quoteLiquidateOut` computes the protocol fee as `_fee = _toLiquidator.wadMul(_protocolFee)`, where `_toLiquidator` is denominated in the deposit token's underlying units (e.g., 8-decimal BTC derivatives or 6-decimal USDC). `wadMul` rounds down, so for any liquidation where the seized collateral amount times the protocol fee rate is below 1 underlying unit, `_fee` truncates to 0. Because `Pool.liquidate` can be called repeatedly with small `amountToRepay_`, a liquidator can drain an entire unhealthy position in dust-sized chunks while paying zero protocol fee.

### Finding Description
In `Pool.sol` `quoteLiquidateOut` (Pool.sol:451-472):

```solidity
_toLiquidator = masterOracle().quote(synthetic, underlying, amountToRepay_);
if (_protocolFee > 0) {
    _fee = _toLiquidator.wadMul(_protocolFee);   // rounds down to 0 for small amounts
}
```

`_toLiquidator` is in the underlying's native decimals. For a WBTC-like collateral (8 decimals) with `_protocolFee = 2%` (2e16), `_fee` is 0 whenever `_toLiquidator < 50` base units; for USDC-like collateral (6 decimals) it is 0 whenever `_toLiquidator < 50` base units as well (`50 * 2e16 / 1e18 = 0`). Synthetic amounts quoted by the oracle naturally map small repayments to dust underlying amounts.

In `liquidate` (Pool.sol:537-596) the only guards are:
- `amountToRepay_ == 0` reverts (dust > 0 is fine)
- `_msgSender == account_` reverts — easily bypassed using a second attacker address
- `amountToRepay_.wadDiv(debtBalance) <= maxLiquidable` — bounds the *fraction* per call, not the absolute size, so arbitrarily small dust is allowed
- `debtFloorInUsd` check — only constrains the *remaining* debt; the attacker can chunk down to 0 final debt on the last call, or keep each remainder above the floor until the final full liquidation

Since `_fee == 0`, the `depositToken_.seize(account_, feeCollector, _fee)` block at Pool.sol:591-593 is skipped entirely. The liquidator still receives `_toLiquidator` including the full `liquidatorIncentive`.

### Impact Explanation
The protocol fee on liquidations can be driven to zero on every liquidation for low-decimal collateral. An attacker operating two addresses (unhealthy position on A, liquidator B) fragments the repayment so each seized chunk is below the rounding threshold, collecting the full liquidator incentive while the fee collector receives nothing. Across repeated liquidations and all users' positions, this is a permanent, repeatable loss of protocol revenue — i.e., theft of yield owed to the fee collector. The rounding is repeatable in the caller's favor, exactly the bug class of the reference report.

### Likelihood Explanation
- Requires an unhealthy position: achievable in normal market volatility, or deliberately by borrowing near the issuable limit before a price move.
- Requires low-decimal collateral: Metronome deployments list BTC-derivative and stablecoin collaterals (e.g., deployments on optimism/base/mainnet include WBTC/USDC-backed deposit tokens).
- Per-chunk seized amount must satisfy `_toLiquidator * _protocolFee < 1e18`. For 6–8 decimal underlyings this corresponds to chunks worth a few cents to a few dollars — thousands of calls may be needed for large positions, but each call is a simple `liquidate` and the saved fee is proportional to the whole position, not the chunk size.
- No privileged role, oracle manipulation, or malicious gateway is needed; `liquidate` is permissionless and `whenNotShutdown`/`nonReentrant` do not restrict it.

### Recommendation
Enforce a minimum non-zero fee, e.g. round the fee up (`wadMulUp`) or revert/skip liquidations below a minimum seized amount:

```solidity
_fee = _toLiquidator.wadMulUp(_protocolFee);
// or: require(_toLiquidator >= MIN_SEIZE_AMOUNT)
```

Alternatively enforce a minimum `amountToRepay_` (in USD terms via the oracle) in `liquidate` so dust-chunk liquidations are impossible.

### Proof of Concept
Foundry-style sketch:

```solidity
// Setup: USDC-like collateral (6 decimals), protocolFee = 5e16 (5%), liquidatorIncentive = 10%
// accountA deposits 1000e6 USDC, issues msUSD near limit; oracle moves so position is unhealthy.

uint256 debt = debtToken.balanceOf(accountA); // e.g. 800e18 msUSD
uint256 chunk = debt / 10000;                 // each repay seizes ~0.1 USDC worth

vm.startPrank(attackerB);
// attackerB holds msUSD (bought/issued)
while (debtToken.balanceOf(accountA) > finalFullRepay) {
    uint256 seizeChunk = pool.quoteLiquidateOut(msUSD, chunk, usdcDepositToken).totalToSeize;
    // seizeChunk ~ 1e5 -> _fee = 1e5 * 5e16 / 1e18 = 0
    pool.liquidate(msUSD, accountA, chunk, usdcDepositToken); // feeCollector receives 0
}
pool.liquidate(msUSD, accountA, debtToken.balanceOf(accountA), usdcDepositToken);

assertEq(usdcDepositToken.balanceOf(feeCollector), 0); // all protocol fees evaded
```

Key assertion: `depositToken.balanceOf(feeCollector)` remains 0 (or orders of magnitude below `expected fee`) after fully liquidating the position, while `attackerB` received the full incentive payout. This is reproducible on a mainnet/optimism fork using the deployed `Pool`, `DepositToken`, `DebtToken`, and `FeeProvider` from `deployments/`.