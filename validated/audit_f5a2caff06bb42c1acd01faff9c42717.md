Found the reachable analog: `system-repay` divides by `debt` (total debt) with no zero-guard, and it's reachable by any principal repaying a vault position.

### Title
Unsafe division by zero debt in `system-repay` causes reverting/DoS on repayment path - (File: mainnet/contracts/vault/v0-vault-stx.clar and sibling vaults)

### Summary
`system-repay` computes `principal-repaid` via `(mul-div-down capped-amount total-borrowed-amount debt)` where `debt` is `(total-debt)`, the current vault's total outstanding debt. Neither `mul-div-down` nor `calc-principal-ratio-reduction` (which also divides by the `debt-amount` argument) checks that the divisor is non-zero before dividing, consistent with the reported bug class (unsafe `rdivide`/`wdivide`-style functions that assume `y != 0`).

### Finding Description
`mul-div-down` is defined as a raw division with no zero check: [1](#0-0) 
`calc-principal-ratio-reduction` divides by its `debt-amount` parameter with no guard: [2](#0-1) 
`system-repay` calls both of these with `debt` (i.e., `(total-debt)`) as the divisor, and also divides directly by `debt` a second time for `principal-repaid`: [3](#0-2) 

If `debt` is `u0` (vault has no outstanding debt — e.g., normal empty-vault state, or after `socialize-debt`/full repayment reduces the vault's debt to zero), any principal-role caller invoking `system-repay` with `amount > 0` triggers `(mul-div-down capped-amount scaled-principal u0)` inside `calc-principal-ratio-reduction`, and separately `(mul-div-down capped-amount total-borrowed-amount u0)` for `principal-repaid` — both divide by zero and abort/revert the transaction. This is the exact bug class from the external report: `rdivide`/`wdivide`-style helpers accepting an unchecked divisor `y` that can be `0`.

The same pattern (`calc-principal-ratio-reduction`, `mul-div-down .. debt`) is duplicated identically across all vaults: `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, so the issue is systemic to the vault family, not vault-specific.

### Impact Explanation
This lands as a Temporary Freezing of Funds (High): a vault whose total debt has fully accrued down to zero (e.g., last borrower fully repaid, or debt fully socialized) becomes permanently unable to accept any `system-repay` call (reverts every time) until new debt is created via a fresh borrow. Since `system-repay` is a routing target invoked by the market entry point (`vault-system-repay` in `v0-4-market.clar`) for closing out user positions, this can block user repayment flows for a vault in a zero-debt state, temporarily freezing any in-flight repay/close operations that route through this vault until borrow activity resumes. It does not by itself cause fund loss or permanent freezing since the vault becomes callable again as soon as `total-debt` is nonzero.

### Likelihood Explanation
Likelihood is moderate: `total-debt` reaching exactly `u0` for a given vault is a normal, easily reachable state (e.g., the last outstanding borrower fully repays their debt, or a vault is newly deployed/emptied), and no special privilege or attacker action is required — an ordinary principal simply calling `system-repay` on an empty-debt vault triggers the revert.

### Recommendation
Add an explicit zero-check on `debt` (and analogously on `scaled-principal`/other divisors) in `calc-principal-ratio-reduction` and in `system-repay` before performing the divisions — e.g., short-circuit to `u0`/no-op when `debt` is `u0`, mirroring the existing zero-guard already present in `calc-utilization` for its `total` divisor: [4](#0-3) 
Apply the same fix to `calc-principal-ratio-reduction`/`system-repay` in all six vault contracts (`v0-vault-stx`, `v0-vault-sbtc`, `v0-vault-ststx`, `v0-vault-ststxbtc`, `v0-vault-usdc`, `v0-vault-usdh`).

### Proof of Concept
1. Ensure a vault (e.g., `v0-vault-stx`) reaches `total-debt = u0` (all outstanding borrowers have fully repaid, or `socialize-debt` has zeroed the debt).
2. Any principal calls `system-repay` (directly, or via the market's `vault-system-repay` router) with `amount > 0` on that vault.
3. Inside `system-repay`, `debt` evaluates to `u0`; `calc-principal-ratio-reduction capped-amount scaled-principal u0` executes `(mul-div-down capped-amount scaled-principal u0)` → division by zero → the transaction aborts, reverting the repay attempt (and the identical `mul-div-down capped-amount total-borrowed-amount u0)` for `principal-repaid` would revert as well). [5](#0-4)

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L159-160)
```text
(define-private (mul-bps-down (x uint) (y uint)) 
  (/ (* x y) BPS))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L164-168)
```text
(define-private (calc-utilization (available-liquidity uint) (debt-amount uint))
  (let ((total (+ debt-amount available-liquidity)))
    (if (is-eq total u0)
        u0
        (mul-div-down debt-amount BPS total))))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L191-192)
```text
(define-private (calc-principal-ratio-reduction (amount uint) (scaled-principal uint) (debt-amount uint))
  (mul-div-down amount scaled-principal debt-amount))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L902-916)
```text
(define-public (system-repay (amount uint))
  (let (
        (states (var-get pause-states))
        (u (try! (accrue)))
        (scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (debt (total-debt))
        (total-borrowed-amount (var-get total-borrowed))
        (capped-amount (if (> amount debt) debt amount))
        (principal-reduction (calc-principal-ratio-reduction capped-amount scaled-principal debt))
        (capped-reduction (if (> principal-reduction scaled-principal) scaled-principal principal-reduction))
        (updated-scaled-principal (- scaled-principal capped-reduction))
        (principal-repaid (mul-div-down capped-amount total-borrowed-amount debt))
        (interest-paid (- capped-amount principal-repaid))
        (total-borrowed-new (if (> total-borrowed-amount principal-repaid) (- total-borrowed-amount principal-repaid) u0)))
```
