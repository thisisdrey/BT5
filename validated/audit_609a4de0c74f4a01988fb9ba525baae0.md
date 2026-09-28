### Title
Liquidator burns repayment synth but can receive zero collateral in `liquidate` due to oracle-quote rounding — (`contracts/Pool.sol`)

### Summary
`Pool.liquidate` derives the seized collateral from `quoteLiquidateOut`, which relies on `masterOracle().quote(synth → underlying, amountToRepay_)` and floors via `wadMul`. For a positive but small `amountToRepay_`, the oracle quote can return `0`, making `_toLiquidator` and `_totalSeized` equal to `0`. The function still executes `syntheticToken_.burn(_msgSender, amountToRepay_)`, so the liquidator's repayment tokens are destroyed while they receive nothing — the same "pay deposit, receive zero shares" rounding defect as the Notional `deleverageAccount` finding. There is no `_totalSeized > 0` / `_toLiquidator > 0` guard.

### Finding Description
The liquidation path is:

```solidity
// Pool.sol ~L581-593
(_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

if (_totalSeized > depositToken_.balanceOf(account_)) {
    revert AmountIsTooHigh();
}

syntheticToken_.burn(_msgSender, amountToRepay_);   // burned regardless
_debtToken.burn(account_, amountToRepay_);
depositToken_.seize(account_, _msgSender, _toLiquidator);   // can be 0
```

`quoteLiquidateOut` computes:

```solidity
// Pool.sol ~L456-471
_toLiquidator = masterOracle().quote(
    address(syntheticToken_),
    address(depositToken_.underlying()),
    amountToRepay_
);
...
_toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive);
_totalToSeize = _fee + _toLiquidator;
```

Master-oracle quotes are `amount * price / scale` style computations that round down; for `amountToRepay_` whose collateral-denominated value is below 1 wei of the deposit token, `_toLiquidator` is `0`. `DepositToken.seize` → `_transfer` does not revert on `amount_ == 0` (the zero checks are only on the zero address and on `amount_ > balance`), so the whole call succeeds. The only zero check is `amountToRepay_ == 0`, not on the output side.

The preconditions are all reachable by any EOA: `whenNotShutdown`, `nonReentrant`, token existence checks, `account_` unhealthy, `amountToRepay_ / debt <= maxLiquidable`, and the `debtFloorInUsd` check (satisfiable by repaying down to exactly 0 or leaving debt above the floor). None of them constrain the seized output to be non-zero.

### Impact Explanation
A liquidator loses their repaid synthetic tokens: `syntheticToken_.burn` destroys real (minted/held) value while `seize` transfers nothing, and the borrower's debt is forgiven without any collateral leaving the position. This is a direct loss of user funds caused purely by output-side rounding — identical in structure to the reference finding where `vaultSharesToLiquidator` floors to 0 while the deposit is taken. Beyond self-harm, the borrower gains free debt relief (debt burned, collateral untouched), i.e. value is transferred from the liquidator to the borrower.

### Likelihood Explanation
Medium-low. It requires a liquidator (or a keeper/bot calling `liquidate` without first checking `quoteLiquidateOut`) to pass an `amountToRepay_` whose oracle-quoted collateral value rounds to 0 — plausible with high-priced synths repaid against low-priced per-unit collateral or dust repayment amounts. No privileged role, oracle corruption, or same-block price manipulation is needed; the rounding is inherent to the quote math.

### Recommendation
Revert when the computed seize output is zero, e.g. in `Pool.liquidate` add `if (_toLiquidator == 0) revert AmountIsZero()` (or equivalent) before burning the liquidator's synth, and/or enforce a minimum `amountToRepay_` so that `quoteLiquidateOut` cannot return 0 for a positive input. Callers can also be protected by documenting that `quoteLiquidateOut` must be consulted first.

### Proof of Concept
Hardhat (repo already uses Hardhat + the deployed test scaffolding in `test/Pool.test.ts`):

```ts
// Setup mirrors existing liquidation tests:
// 1. alice deposits MET (msdMET) and mints msETH up to the issuable limit.
// 2. Drop MET price so alice's position becomes unhealthy (_isHealthy == false).

// Exploit step: pick amountToRepay such that quote(synth -> underlying) == 0
const depositToken = msdMET // e.g. an underlying whose wei-price exceeds the synth wei-price
// find smallest amountToRepay with non-zero quote:
const debt = await msEthDebtToken.balanceOf(alice.address)

// choose a dust amountToRepay below the oracle's quantization for the pair
const amountToRepay = 1 // or minimal wei such that quote returns 0
const [seizedBefore, toLiqBefore] = await pool.quoteLiquidateOut(
  msEth.address, amountToRepay, msdMET.address)
// precondition of the bug: positive repay, zero seized
expect(amountToRepay).gt(0)
expect(toLiqBefore).eq(0)

const liqBalBefore = await msEth.balanceOf(liquidator.address)
const aliceDebtBefore = await msEthDebtToken.balanceOf(alice.address)
const aliceCollBefore = await msdMET.balanceOf(alice.address)

await pool.connect(liquidator).liquidate(
  msEth.address, alice.address, amountToRepay, msdMET.address)

// liquidator lost synth, received nothing; borrower's debt reduced for free
expect(await msEth.balanceOf(liquidator.address)).eq(liqBalBefore.sub(amountToRepay))
expect(await msdMET.balanceOf(liquidator.address)).eq(liqCollBefore) // +0
expect(await msdMET.balanceOf(alice.address)).eq(aliceCollBefore)    // unchanged
expect(await msEthDebtToken.balanceOf(alice.address)).eq(aliceDebtBefore.sub(amountToRepay))
```

The exact dust amount depends on the deployed oracle pair's price/decimal scaling (`contracts/mock/MasterOracleMock.sol` can emulate floor-rounding in a fork/unit test); on a mainnet fork it is determined empirically by scanning `quoteLiquidateOut` for inputs where `_totalToSeize == 0` while `amountToRepay_ > 0`, then confirming `liquidate` succeeds and burns the repaid amount.