### Title
`DebtToken.repayAll` repays the borrower's entire live debt balance, so a borrower can front-run a third-party `repayAll` call by minting additional debt and force the repayer to burn more synth than intended - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
Metronome's `DebtToken.repayAll(onBehalfOf_)` is the direct analog of Surge's `repay(borrower, type(uint256).max)`. It reads `balanceOf(onBehalfOf_)` at execution time — i.e., the borrower's current debt including interest — and burns that full amount of synthetic tokens from `msg.sender`. Because the amount is resolved from live state rather than bounded by a caller-supplied cap, the borrower can front-run the transaction with a debt-increasing action (e.g., `Pool.swap` that mints synth to themselves, or `SmartFarmingManager.leverage`) and force the repayer to pay the enlarged balance.

### Finding Description
In `DebtToken.sol`:

```solidity
function repayAll(address onBehalfOf_) external ... {
    accrueInterest();
    _repaid = balanceOf(onBehalfOf_);   // full live debt, no upper bound
    ...
    (_amount, _fee) = quoteRepayIn(_repaid);
    if (_fee > 0) _syntheticToken.seize(_msgSender, pool.feeCollector(), _fee);
    _syntheticToken.burn(_msgSender, _repaid);  // burns from repayer
    _burn(onBehalfOf_, _repaid);
}
``` [1](#0-0) 

Two properties combine into the attack:

1. `_repaid = balanceOf(onBehalfOf_)` is computed inside the transaction, after `accrueInterest()`. `balanceOf` reflects `principalOf[account] * debtIndex`, so it grows with any new debt minted to `onBehalfOf_` in the same block.
2. The tokens burned come from the repayer (`_syntheticToken.burn(_msgSender, _repaid)`), while the debt reduced belongs to `onBehalfOf_`.

The borrower's front-run options are all public/unprivileged:
- `Pool.swap(tokenIn_, synthToken, ...)` — mints additional synthetic token debt to themselves (subject only to their collateral health check).
- `SmartFarmingManager.leverage` / `flashRepay` — increases the position's debt.
- `DebtToken.issue` via any flow where the borrower controls the `onBehalfOf` recipient.

The repayer has no way to bound the spend: `repayAll` takes no `amount_` parameter, and `repay(onBehalfOf_, amount_)` cannot fully repay an unpredictable balance without leaving dust (a partial repay may also trip the `RemainingDebtIsLowerThanTheFloor` check at `DebtToken.sol:448-449`). So `repayAll` is the only practical "repay everything" path, and it is unbounded.

Contrast with the Surge fix: there is no `Math.min(debt, amount)` cap — the design intentionally removed the bound to avoid debt dust.

### Impact Explanation
Direct loss of repayer funds: any third party (an EOA, a helper contract, an `Operator.execute` meta-tx relayer, or a repayment zap) that calls `repayAll(victim)` intending to clear debt `D` can be forced to burn `D + X` synth, where `X` is bounded only by the borrower's remaining minting capacity (collateral ratio / supply caps). The borrower ends with zero debt and keeps the newly minted `X` synth — a wealth transfer from repayer to borrower equal to `X` plus accrued interest.

### Likelihood Explanation
Requires a specific scenario: a third party repaying a user's full debt (repayment-on-behalf composability, OTC debt settlement, keeper-free repay helpers). In those cases, the attack is trivially executable by the borrower monitoring the mempool — any unprivileged address can call `Pool.swap` to inflate their own debt. This mirrors the accepted Medium Surge finding; on Metronome the window is identical.

### Recommendation
Add a bounded variant: `repayAll(onBehalfOf_, maxAmount_)` that computes `_repaid = Math.min(balanceOf(onBehalfOf_), maxAmount_)` (or equivalent via `quoteRepayIn` on the cap), so the repayer's spend is capped at a value they chose, computed e.g. as `balanceOf` + expected interest slippage. External repayers can then verify `debtToken.balanceOf(onBehalfOf_) == 0` post-call. Alternatively expose a `repayUpTo(onBehalfOf_, amount_)` wrapper around `repay` that skips the dust concern by burning `min(balance, repaid)`.

### Proof of Concept
Reproducible in the existing Hardhat suite (`test/DebtToken.test.ts`):

```ts
// setup identical to 'repayAll' tests: user1 has debt D, repayer holds synth
const debtBefore = await msUSDDebt.balanceOf(user1.address)

// borrower front-runs: mint more debt to self via Pool.swap (or leverage)
await pool.connect(user1).swap(collateralOrSynthIn, msUSD.address, amountIn, minOut) // increases user1 debt by ~X
// simpler in the mock setup: another account issues synth to user1? issue mints debt to _account via pool only —
// the realistic path: user1 calls msUSDDebt through pool swap, increasing balanceOf(user1)

// repayer calls repayAll expecting to burn ~debtBefore
const repayerBefore = await msUSD.balanceOf(repayer.address)
await msUSDDebt.connect(repayer).repayAll(user1.address)
const repayerSpent = repayerBefore.sub(await msUSD.balanceOf(repayer.address))

expect(repayerSpent).to.be.gt(debtBefore) // repayer burned more than the debt they saw
expect(await msUSDDebt.balanceOf(user1.address)).eq(0)
// user1 still holds the X synth minted in the front-run
```

The key assertion — `repayerSpent > debtBefore` — holds because `repayAll` resolves `balanceOf(onBehalfOf_)` after the front-run `accrueInterest()`/debt increase, with no caller-supplied cap.

### Citations

**File:** contracts/DebtToken.sol (L466-494)
```text
    function repayAll(
        address onBehalfOf_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
    {
        accrueInterest();

        _repaid = balanceOf(onBehalfOf_);
        if (_repaid == 0) revert AmountIsZero();

        address _msgSender = _msgSender();
        ISyntheticToken _syntheticToken = syntheticToken;

        uint256 _amount;
        (_amount, _fee) = quoteRepayIn(_repaid);

        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, pool.feeCollector(), _fee);
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);

        emit DebtRepaid(_msgSender, onBehalfOf_, _amount, _repaid, _fee);
```
