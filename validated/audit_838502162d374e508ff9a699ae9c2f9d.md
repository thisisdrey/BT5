### Title
Bad debt is never written off — zombie `DebtToken` balance keeps accruing interest that is minted as real, unbacked synthetic tokens to `feeCollector` - (File: contracts/DebtToken.sol)

### Summary
When a liquidation leaves an account with zero (or dust) collateral but residual debt, Metronome has no mechanism to write off or socialize that bad debt. The leftover `DebtToken` principal keeps accruing interest via `accrueInterest()`, and the accrued amount is minted as real `SyntheticToken` to `feeCollector` — i.e., the protocol continuously mints unbacked synthetic tokens against debt that can never be repaid.

### Finding Description
In `Pool.liquidate`, repayment is capped so that `_totalSeized <= depositToken_.balanceOf(account_)` (`Pool.sol:583`), and `quoteLiquidateMax` only repays up to what the remaining collateral can cover (`Pool.sol:435-439`). For an underwater account, the liquidator seizes ~all collateral while a positive `DebtToken` balance remains — this is even asserted in tests (`Pool.test.ts:813-815`, `_debtToken.balanceOf(alice) > 0` after the full deposit was seized).

There is no `_processDefault`/write-off/socialization path anywhere in `Pool`, `DepositToken`, or `DebtToken`. The zombie principal stays in `totalSupply_`, so `_calculateInterestAccrual` keeps growing `debtIndex` on it (`DebtToken.sol:562`), and `accrueInterest()` mints `_interestAmountAccrued + pendingInterestFee` as real `SyntheticToken` to `pool.feeCollector()` (`DebtToken.sol:169-178`). `collectPendingInterestFee()` (`DebtToken.sol:220-226`) is permissionless, so anyone can force the mint even when the `try` mint fails. `debtFloorInUsd` only prevents repayment that would leave dust — it never clears the debt.

### Impact Explanation
Protocol insolvency / unbacked dilution: synthetic supply minted to `feeCollector` is backed nominally by debt interest, but the corresponding debt is uncollectible (the account has no collateral left to seize, and no party will voluntarily repay it). Every second, interest on zombie debt is converted into spendable synth held by the fee collector, while `totalSupply_` grows without any collateral behind it — the synth-backing invariant (synth supply ≙ collectible debt) is permanently broken and worsens over time. If the fee collector sells or swaps the minted synths via `Pool.swap`, real collateral/synth value is extracted against non-existent backing.

### Likelihood Explanation
Only requires a market move making any position's collateral worth less than its debt — the normal tail event liquidations are designed for. Tests already demonstrate the state (`_isHealthy == false`, deposit ~0, debt > 0) is reachable and persists. An unprivileged attacker can also engineer this deliberately: deposit a volatile collateral, mint near max, and let/or push the price down (same-transaction oracle/AMM manipulation is in-scope) so `liquidate` leaves residual debt; thereafter the debt is a permanent unbacked-interest faucet. No privileged role, keeper, or oracle-operator misbehavior is needed for the accounting flaw itself.

### Recommendation
Add a bad-debt write-off: when `liquidate` (or a dedicated `settleBadDebt`) reduces an account's collateral to zero while debt remains, burn the residual `DebtToken` principal without minting synth, and exclude written-off principal from `totalSupply_`/`debtIndex` accrual (or socialize it explicitly). Alternatively, track bad debt separately so `accrueInterest` does not mint fees against uncollectible debt.

### Proof of Concept
Hardhat (repo test style), fork or local fixture with `pool`, `msEth`/`msdMET`, `masterOracle` mock as in `test/Pool.test.ts`:

```ts
// given: alice deposits MET, issues msEth; MET price drops so debt > collateral
await masterOracle.updatePrice(met.address, toUSD('0.50'));

// when: liquidator repays the max the collateral can cover
const amountToRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address);
await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address);

// residual bad debt remains (state shown in Pool.test.ts:811-815)
expect(await msdMET.balanceOf(alice.address)).closeTo(0, 2000);
const zombieDebt = await msEthDebtToken.balanceOf(alice.address);
expect(zombieDebt).gt(0);

// interest keeps accruing on the uncollectible debt and is minted to feeCollector
const feeBalBefore = await msEth.balanceOf(poolRegistryMock.feeCollector());
await ethers.provider.send('evm_increaseTime', [365 * 24 * 3600]); // 1 year
await msEthDebtToken.accrueInterest();
const feeBalAfter = await msEth.balanceOf(poolRegistryMock.feeCollector());

// then: real synth was minted to feeCollector backed only by zombie debt interest
expect(feeBalAfter.sub(feeBalBefore)).gt(0);
// alice's debt grew further while collateral stays ~0 — nothing clears it
expect(await msEthDebtToken.balanceOf(alice.address)).gt(zombieDebt);
```