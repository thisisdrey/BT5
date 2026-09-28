### Title
Debt balance out-of-sync with stored total supply causes `totalSupply_` underflow (unchecked) in `_burn`, permanently bricking the DebtToken - (File: contracts/DebtToken.sol)

### Summary
`DebtToken._burn` computes the burn allowance against the *live* accrued debt (`balanceOf` includes pending interest), but subtracts the burn amount from the *stored* `totalSupply_` inside an `unchecked` block. The repay path (`repay`/`repayAll` → `_burn`) does not call `accrueInterest()` first, so `totalSupply_` lags `balanceOf` by the unaccrued interest. Repaying an amount in `(totalSupply_, balanceOf]` wraps `totalSupply_` to ~2^256, after which `accrueInterest` overflows in `wadMul` and `_mint` reverts on `SurpassMaxDebtSupply` — permanently freezing minting, liquidation and interest accrual for the synthetic asset.

### Finding Description
`balanceOf` returns `principalOf * liveDebtIndex / debtIndexOf[account]`, i.e. it includes interest accrued since `lastTimestampAccrued` without writing it back (`contracts/DebtToken.sol:196-206`). `totalSupply_` is only advanced when `accrueInterest()` executes (`contracts/DebtToken.sol:156-180`).

In `_burn` (`contracts/DebtToken.sol:525-543`):

```solidity
uint256 _accountBalance = balanceOf(account_);            // includes pending interest
if (_accountBalance < amount_) revert BurnAmountExceedsBalance();

unchecked {
    principalOf[account_] = _accountBalance - amount_;
    debtIndexOf[account_] = debtIndex;
    totalSupply_ -= amount_;                              // stored supply EXCLUDES pending interest
}
```

`Pool.liquidate` calls `_debtToken.accrueInterest()` before `burn`, which synchronises `totalSupply_` with `balanceOf` (`contracts/Pool.sol:557`). The `repay`/`repayAll` path (`contracts/DebtToken.sol:481-495`) does **not** accrue interest before `_burn`, so for any account `balanceOf(account) > totalSupply_` whenever `totalSupply_` is dominated by that account's principal and interest is pending. Because the subtraction is `unchecked`, `totalSupply_` wraps to `type(uint256).max - delta` instead of reverting.

After the wrap:
- `accrueInterest()` → `_calculateInterestAccrual` → `_interestRateToAccrue.wadMul(totalSupply_)` overflows inside `wadMul` and always reverts (`contracts/DebtToken.sol:551-566`), permanently bricking `Pool.liquidate` (which calls `accrueInterest` first), `updateInterestRate`, and any path touching accrual.
- `_mint` reverts at `if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply()` (`contracts/DebtToken.sol:591`), permanently disabling issuance.

This mirrors the reported bug class: a crafted numeric operand (repay amount) and a scale mismatch (accrued `balanceOf` vs stale stored `totalSupply_`) produce an out-of-bounds write to a core accounting variable.

### Impact Explanation
Protocol insolvency and permanent freezing of funds. Once `totalSupply_` is wrapped:
- `accrueInterest` reverts forever → `Pool.liquidate` reverts → unhealthy positions cannot be liquidated, so bad debt accumulates and no collateral can be seized.
- `issue`/`_mint` revert → the synthetic token is dead.
- `DebtToken.burn` itself still works but subtracts from the wrapped value, so `totalSupply()` and interest accounting are permanently corrupted; the recorded debt can never be reconciled with actual obligations.

The condition is reachable by any unprivileged user who is (or aggregates) the dominant debtor of a DebtToken, waits for pending interest δ > 0, obtains δ additional synthetic tokens (mint via a second position or buy on market), and repays `X + δ` where `X = totalSupply_`.

### Likelihood Explanation
- Fully attacker-reachable via the public `repay`/`repayAll` entry points; no privileged role, oracle manipulation, or governance parameter needed.
- Requires only that pending (unaccrued) interest exists — true at almost any time since accrual is lazy — and that the attacker controls enough synthetic tokens to cover the accrued delta, which they can mint themselves.
- Once triggered it is irreversible: there is no function that resets `totalSupply_`.
- The main residual uncertainty is whether `repay` performs an `accrueInterest()` call earlier in its body than the indexed snippet shows (lines 481-495 show none); `liquidate` explicitly calls it, strongly suggesting repay intentionally does not.

### Recommendation
- Remove `unchecked` arithmetic from `_burn`, or accrue interest before computing balances: call `accrueInterest()` at the top of `repay`, `repayAll`, and any path that reaches `_burn` (matching the `liquidate` pattern).
- Alternatively, track supply consistently: burn against the same basis used for `totalSupply_` (e.g. compute `_accountBalance` from the stored index, or always write accrued interest into `totalSupply_` before the subtraction).

### Proof of Concept
Hardhat fork test sketch:

```ts
// Setup: pool, msUSD + msUSDDebt deployed; alice deposits collateral and issues.
// Assume alice is the only debtor: totalSupply_ == principal.
await depositToken.connect(alice).deposit(collateralAmount, alice.address);
await msUSDDebt.connect(alice).issue(mintAmount, alice.address); // debt = mintAmount

// 1. Let interest accrue off-chain (no accrueInterest call):
await time.increase(365 * 24 * 3600); // e.g. 1 year at 10% APR => balanceOf ≈ 1.1 * mintAmount

const storedSupply = await msUSDDebt.totalSupply(); // view adds accrual; use raw slot or
// compute: totalSupply_ (stored) == mintAmount, balanceOf(alice) > mintAmount
const debt = await msUSDDebt.balanceOf(alice.address);
const delta = debt.sub(mintAmount);
expect(delta).gt(0);

// 2. Attacker obtains `delta` extra msUSD (second position or market) and repays
//    an amount _repaid > stored totalSupply_ but <= balanceOf:
const repayGross = /* gross amount such that _repaid == debt */;
await msUSDDebt.connect(alice).repay(alice.address, repayGross);

// 3. totalSupply_ has wrapped to ~type(uint256).max:
//    every subsequent state-changing call that accrues reverts:
await expect(msUSDDebt.accrueInterest()).to.be.reverted;          // wadMul overflow
await expect(
  msUSDDebt.connect(bob).issue(1, bob.address)
).to.be.revertedWithCustomError(msUSDDebt, 'SurpassMaxDebtSupply');
await expect(
  pool.connect(liquidator).liquidate(msUSD.address, victim.address, 1, depositToken.address)
).to.be.reverted;                                                  // accrueInterest overflows
```

Expected result: `accrueInterest` and `issue` revert permanently and `Pool.liquidate` is unusable for that synthetic token — permanent freezing of funds and protocol insolvency, reproducible in a single transaction after interest has been left unaccrued.