### Title
Half-up rounding in `wadMul`/`wadDiv` lets a liquidator seize up to 1 wei more collateral than owed per liquidation - (File: contracts/Pool.sol)

### Summary
`Pool.liquidate` prices the seized collateral through `quoteLiquidateIn`/`quoteLiquidateOut`, which use `WadRayMath.wadMul`/`wadDiv` — both round half-up rather than in the protocol's favor. An unprivileged liquidator can choose `amountToRepay_` such that the fractional part of `_repayAmountInCollateral.wadMul(1e18 + _liquidatorIncentive)` is ≥ 0.5 wei, deterministically rounding their seized share up by ~1 wei at the liquidated account's expense. This is the same bug class as the referenced report: an inverse/fee-split computation rounding in the caller's favor when the attacker controls the amount input.

### Finding Description
In `Pool.liquidate` (`contracts/Pool.sol:537`), the amount seized is computed as:

- `quoteLiquidateOut` (`contracts/Pool.sol:451-471`): `_toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive)` and `_fee = _toLiquidator.wadMul(_protocolFee)`, then `_totalToSeize = _fee + _toLiquidator`.
- `quoteLiquidateIn` (`contracts/Pool.sol:383-409`): `_repayAmountInCollateral.wadDiv(1e18 + _totalFees)` and `_repayAmountInCollateral.wadMul(1e18 + _liquidatorIncentive)`.

`WadRayMath.wadMul` computes `(a * b + HALF_WAD) / WAD` and `wadDiv` computes `(a * WAD + b / 2) / b` — both round half-up (`contracts/lib/WadRayMath.sol:25-41`). There is no rounding-down variant used anywhere in the liquidation or swap path. In `quoteLiquidateOut` the rounding direction benefits the liquidator: their share is rounded up, and since `amountToRepay_` is attacker-controlled and the liquidation incentive is known on-chain, the attacker can pick an input that forces the ≥0.5 wei case every call. The protocol fee term also rounds up, so `_totalToSeize` can exceed the oracle-fair seizure by up to ~2 wei, all deducted from the victim's `DepositToken` balance via `DepositToken.seize`.

The analogous swap path (`Pool.swap` → `quoteSwapOut`, `contracts/Pool.sol:508-525`) does *not* exhibit the flaw in the caller's favor: with zero swap fee there is no rounding at all (output = oracle quote), and with a fee the `wadMul` rounds the fee up, reducing user output — so `liquidate` is the strongest reachable surface.

### Impact Explanation
Direct theft of user funds, bounded to ~1 wei of collateral per liquidation call. The liquidated account loses slightly more collateral than the oracle value of the repaid debt plus incentive. Unlike the reference bug, this does not require zero-fee configuration — it requires only `_liquidatorIncentive > 0`, which is the normal deployed configuration. The magnitude is dust-level (≤1 wei/call) and the per-call gain is bounded by `maxLiquidable` and gas costs, so aggregate theft is limited; however the rounding direction is deterministic, attacker-controllable, and systematically favors the caller rather than the protocol.

### Likelihood Explanation
Any EOA can trigger it: wait for (or create via a same-transaction oracle move) an unhealthy position, then call `Pool.liquidate(syntheticToken_, victim, craftedAmountToRepay_, depositToken_)`. `liquidate` is permissionless (`whenNotShutdown`, `nonReentrant`, `onlyIfSyntheticTokenExists`, `onlyIfDepositTokenExists` — none block this). The attacker chooses `amountToRepay_` to force the round-up branch. Prerequisites: an unhealthy position must exist and the liquidation incentive must be non-zero. Profit per call is ~1 wei, so practical exploitation requires no gas constraint or aggregation across many liquidations; the finding is real but economically marginal.

### Recommendation
Round all liquidation and swap fee/share math in the protocol's favor:
- `_toLiquidator` / `_repayAmountInCollateral` for the liquidator's share: round down (`a * b / WAD`).
- `_fee` for the protocol: round up is fine, but compute `_totalToSeize` from the oracle quote directly and derive `_toLiquidator = _totalToSeize - _fee` so rounding never inflates total seizure beyond the oracle value.
- Alternatively, use explicit `Math.mulDiv(a, b, WAD, Rounding.Down)` for any term paid to the caller.

### Proof of Concept
Hardhat sketch (fork or fixture with a live liquidation incentive, e.g. `liquidatorIncentive = 0.1e18`):

```ts
// Pool.liquidate rounding PoC
// Setup: create unhealthy position for victim (deposit collateral, mint msAsset,
// then drop collateral price below liquidation threshold).

const incentive = (await feeProvider.liquidationFees())._liquidatorIncentive; // > 0

// Brute-force amountToRepay so that
// (_repayAmountInCollateral * (1e18 + incentive)) % 1e18 >= 0.5e18
let amountToRepay = parseEther('1');
for (let i = 0; i < 1000; i++) {
  const [, toLiq] = await pool.quoteLiquidateOut(msAsset.address, amountToRepay, depositToken.address);
  const exact = toLiq; // compare against floor-math computation below
  const base = await masterOracle.quote(msAsset.address, underlying.address, amountToRepay);
  const floorVal = base.mul(BigNumber.from(1e18).add(incentive)).div(parseEther('1'));
  if (exact.sub(base).gt(floorVal.sub(base))) break; // found round-up case
  amountToRepay = amountToRepay.add(1);
}

const victimSeizedBefore = await depositToken.balanceOf(victim.address);
await pool.connect(attacker).liquidate(msAsset.address, victim.address, amountToRepay, depositToken.address);
// Assert: seized share == floor(share) + 1 wei — victim loses 1 wei more than oracle-fair amount.
```

Expected: with a crafted `amountToRepay_`, `_toLiquidator` equals the floor value plus 1 wei; repeating across liquidations deterministically skims ≤1 wei of victim collateral per call.

Caveat: the `MasterOracle.quote` implementation is external to this repo (only the interface `contracts/interfaces/external/IMasterOracle.sol` is indexed), so rounding inside the oracle quote itself could not be verified; the finding rests on the Pool-side `wadMul`/`wadDiv` half-up rounding, which is confirmed in-repo.