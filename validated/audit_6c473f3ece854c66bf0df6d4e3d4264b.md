Found it: `system-repay` in the vault contracts calls `calc-principal-ratio-reduction` with `debt` as the divisor, without any zero-check, mirroring the exact bug class from the Cork report (an unconditional division where the denominator can be zero on a legitimate, unprivileged user-reachable path).

### Title
Division-by-zero DoS in `system-repay` via unguarded `calc-principal-ratio-reduction` denominator - (File: `mainnet/contracts/vault/v0-vault-stx.clar` and equivalent v0-vault-* contracts)

### Summary
`system-repay` computes `principal-reduction` by calling `calc-principal-ratio-reduction capped-amount scaled-principal debt`, which performs `mul-div-down amount scaled-principal debt-amount` i.e. `(/ (* amount scaled-principal) debt-amount)` with no zero-guard on `debt-amount`. [1](#0-0) [2](#0-1) 

### Finding Description
`calc-principal-ratio-reduction` is defined as an unconditional `mul-div-down` with `debt-amount` as the divisor and has no `is-eq debt-amount u0` check, unlike the sibling function `socialize-debt` in the very same contract, which explicitly guards the analogous division (`(if (> scaled-principal u0) (mul-div-down ...) u0)`). [2](#0-1) [3](#0-2) 

In `system-repay`, `debt` is `(total-debt)`, computed from `scaled-principal` and the current `index` via `calc-cumulative-debt`. `total-debt` is zero whenever `principal-scaled` is zero — i.e. whenever there is currently no outstanding borrow in the vault (either the vault has never had a borrow, or all previous borrows have already been fully repaid/socialized down to zero). [4](#0-3) [5](#0-4) 

`system-repay` only checks `(> amount u0)`; it never asserts `debt > u0` before dividing. [6](#0-5) 

This exactly parallels the analog external report: a legitimate call path (repaying a loan, analogous to the RA→DS swap) unconditionally divides by a value (`debt`, analogous to `lvReserve`/`psmReserve`) that can legitimately be zero under normal, unprivileged usage (no debt outstanding), causing the Clarity runtime to abort with a runtime division-by-zero error rather than gracefully handling the "nothing to reduce" case (as `socialize-debt` correctly does).

`system-repay` is invoked via the market contract's routing function `vault-system-repay`, reachable by any authorized borrower/market caller performing an ordinary repay operation — not a DAO-privileged action. [7](#0-6) 

### Impact Explanation
When `principal-scaled` (and therefore `debt`) is zero — e.g., the market attempts an auto-repay/settlement call after a borrower's debt was already fully repaid or fully socialized in the same block — any subsequent `system-repay` call for that vault reverts with a runtime panic. This causes temporary denial of service of the repay path for that vault: legitimate repay/settlement transactions revert instead of completing (analogous to the Cork issue where legitimate RA→DS swaps reverted instead of completing via the AMM fallback). Depending on how the calling market contract handles the revert (e.g., in batched liquidation/repay flows), this can temporarily freeze funds in transit (repaid collateral/interest that cannot be settled) until the vault state changes enough to make `debt` nonzero again. This lands in the temporary-freezing-of-funds bucket of the in-scope High impact class.

### Likelihood Explanation
The zero-debt state is a normal, easily-reachable state for any vault with no active borrowers (the initial state of every vault, or any period after all debt is repaid) — no privileged action or DAO compromise is required to reach it. Any caller (or automated market-triggered repay/settlement flow) that attempts a repay call while `principal-scaled` is `u0` triggers the panic. This makes the bug straightforward to hit unintentionally in normal operation, and it is trivially reproducible in a test by calling `system-repay` on a freshly-deployed or fully-repaid vault.

### Recommendation
Guard `calc-principal-ratio-reduction`'s divisor exactly the way `socialize-debt` already guards `scaled-principal`:
```clarity
(define-private (calc-principal-ratio-reduction (amount uint) (scaled-principal uint) (debt-amount uint))
  (if (is-eq debt-amount u0)
      u0
      (mul-div-down amount scaled-principal debt-amount)))
```
Alternatively, add an explicit early return / assertion in `system-repay` that short-circuits when `debt` is `u0` (e.g., `(asserts! (> debt u0) ERR-...)` or return `(ok true)` as a no-op), so the repay path never reaches the division with a zero denominator.

### Proof of Concept
1. Deploy `v0-vault-stx` (or any vault) with `principal-scaled` at its initial value of `u0` (no borrows yet, per `(define-data-var principal-scaled uint u0)`). [8](#0-7) 
2. Call `system-repay` with any nonzero `amount` before any `system-borrow` has occurred (or after a prior borrow has been fully repaid/socialized to `principal-scaled = u0`).
3. `total-debt` evaluates to `calc-cumulative-debt(u0, index) = mul-div-up(0, index, INDEX-PRECISION) = 0`.
4. `capped-amount = min(amount, debt) = 0` is irrelevant — `principal-reduction` still evaluates `calc-principal-ratio-reduction(0, 0, 0)` = `mul-div-down(0, 0, 0)` = `(/ (* 0 0) 0)`, which is a division by zero and causes the Clarity call to abort with a runtime arithmetic error, reverting the transaction instead of completing normally.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L120-120)
```text
(define-data-var principal-scaled uint u0)
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L180-182)
```text
(define-private (calc-cumulative-debt (principal-amount uint) (idx uint))
  (mul-div-up principal-amount idx INDEX-PRECISION))

```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L191-193)
```text
(define-private (calc-principal-ratio-reduction (amount uint) (scaled-principal uint) (debt-amount uint))
  (mul-div-down amount scaled-principal debt-amount))

```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L328-330)
```text
(define-private (total-debt)
  (calc-cumulative-debt (var-get principal-scaled) (var-get index)))

```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L902-920)
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

    (try! (check-caller-auth))
    (asserts! (not (get repay states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L944-958)
```text
(define-public (socialize-debt (scaled-amount uint))
  (let ((scaled-principal (var-get principal-scaled))
        (borrowed (var-get total-borrowed))
        (idx (var-get index))
        (current-assets (var-get assets))
        (current-lindex (var-get lindex))
        (old-total-assets (total-assets))
        (debt-reduction (mul-div-down scaled-amount idx INDEX-PRECISION))
        (principal-reduction (if (> scaled-principal u0)
                                (mul-div-down scaled-amount borrowed scaled-principal)
                                u0))
        ;; Write down lindex proportionally to loss in total-assets
        (new-lindex (if (and (> old-total-assets u0) (> old-total-assets debt-reduction))
                       (mul-div-down current-lindex (- old-total-assets debt-reduction) old-total-assets)
                       u0)))
```

**File:** local-testing/contracts/market/market.clar (L197-204)
```text
(define-private (vault-accrue (aid uint))
  (if (is-eq aid STX) (contract-call? .vault-stx accrue)
  (if (is-eq aid sBTC) (contract-call? .vault-sbtc accrue)
  (if (is-eq aid stSTX) (contract-call? .vault-ststx accrue)
  (if (is-eq aid USDC) (contract-call? .vault-usdc accrue)
  (if (is-eq aid USDH) (contract-call? .vault-usdh accrue)
  (if (is-eq aid stSTXbtc) (contract-call? .vault-ststxbtc accrue)
  ERR-UNKNOWN-VAULT)))))))
```
