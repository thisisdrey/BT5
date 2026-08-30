### Title
`accrue()` treasury-LP mint math can divide by zero, permanently bricking a Zest vault - (File: `mainnet/contracts/vault/v0-vault-*.clar`)

### Summary
### Finding Description
Every Zest vault (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`) implements the same `calc-treasury-lp-preview` helper, used by `total-supply-preview` and directly invoked from the public `accrue` function that runs on every `deposit` and `redeem`: [1](#0-0) 

```clarity
(define-private (calc-treasury-lp-preview)
  (let ((scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (next (next-index))
        (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
        (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
        (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
        (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
        (ta-preview (total-assets-preview)))
    (if (> reserve-inc u0)
        (mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))
        u0)))
```

This is the exact same bug class as the referenced `PrizeVault._convertToShares` division-by-zero (M-17): a `mulDiv`/`mul-div-down` call whose denominator, `(- ta-preview reserve-inc)`, is computed without first guaranteeing it is non-zero. Unlike the `PrizeVault` fix pattern (checking `_totalAssets >= _totalDebt` before dividing), here the code only checks `(> reserve-inc u0)` and unconditionally divides by `ta-preview - reserve-inc` whenever that branch is taken. If `ta-preview == reserve-inc` (a legitimate outcome when accrued interest — and therefore the reserve fee cut of it — is large relative to currently held/idle vault assets, e.g. after a long period without an `accrue` call while `total-borrowed` is high and `assets` (idle liquidity) is low), the division traps in Clarity, aborting the transaction.

`accrue()` is called unconditionally at the top of `deposit`, `redeem`, and other lending operations, e.g.: [2](#0-1) 

```clarity
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      ...
``` [3](#0-2) 

```clarity
(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
    ...
```

Once the zero-denominator condition is hit inside `accrue`, every call to `deposit`, `redeem`, `transfer` (which also calls `accrue`), and any market operation depending on this vault's `accrue` will revert, because Clarity aborts the whole transaction on division by zero rather than returning an error that can be gracefully handled with `asserts!`/`try!`.

### Impact Explanation
This is a temporary/permanent freezing-of-funds vulnerability: once the trapping state (`ta-preview == reserve-inc`) is reached, the vault's `accrue()` reverts on every call, which blocks `deposit`, `redeem`, and `transfer` for that vault (all of which call `accrue` first). Because the index/timestamp state that produces `debt-delta`/`reserve-inc` keeps advancing with time (or with borrow/repay activity in the paired market), the exact denominator relationship can persist or recur, potentially locking depositor funds in the vault with no user-accessible path to `redeem` them. This matches the in-scope "temporary freezing of funds" (and potentially permanent, if the condition cannot self-resolve) impact class for vault share math / interest accrual.

### Likelihood Explanation
The trigger condition requires `ta-preview` (idle assets + accrued interest) to be numerically equal to `reserve-inc` (the protocol's fee cut of newly accrued debt). This is most plausible when idle vault liquidity is thin and utilization/borrow is high with a non-trivial `fee-reserve`, or after a long gap between `accrue()` calls that lets `debt-delta` (and hence `reserve-inc`) grow large relative to `total-assets-preview`. No privileged action is required — any user calling `deposit`/`redeem` at the wrong moment triggers the revert, and repeated interest accrual naturally increases `debt-delta` over time, making the equality condition reachable purely through normal protocol operation (borrowing activity) without any attacker-controlled parameter beyond timing.

### Recommendation
Guard the division the same way `convert-to-shares-preview`/`convert-to-assets-preview` already do elsewhere in the same file, e.g.:
```clarity
(let ((denom (if (> ta-preview reserve-inc) (- ta-preview reserve-inc) u0)))
  (if (and (> reserve-inc u0) (> denom u0))
      (mul-div-down reserve-inc (total-supply) denom)
      u0))
```
This mirrors the Sherlock-reported fix for `PrizeVault._convertToShares`, ensuring `accrue()` (and everything gated behind it) can never revert due to a zero denominator.

### Proof of Concept
Conceptual PoC (cannot execute Clarity locally, so this is a reasoning-based trace, not an executed test):
1. Vault is initialized; `total-borrowed` grows via market borrowing until `assets` (idle liquidity) is small relative to accrued interest.
2. Time passes (or several blocks elapse) without anyone calling a state-changing vault function, so `next-index` diverges significantly from `index`, making `debt-delta` (and `reserve-inc = debt-delta * fee-reserve / BPS`) large relative to `total-assets-preview` (`ta-preview`).
3. A user calls `deposit` or `redeem`. This invokes `accrue()` → `calc-treasury-lp-preview` (via `total-supply-preview`) with `reserve-inc == ta-preview`, hitting `(- ta-preview reserve-inc)` = `u0`.
4. `mul-div-down reserve-inc (total-supply) u0` traps, causing the whole `deposit`/`redeem` transaction to abort.
5. Because `accrue` is called first in every vault operation that could otherwise change the state to escape this condition (e.g., add liquidity), and the state (`index`, `total-borrowed`) that produces the trapping condition persists, subsequent calls continue to abort, freezing user funds in the vault.

Note: I was unable to inspect the exact `mul-div-down` primitive's implementation (its source file was truncated/unavailable in the index) to 100% confirm it traps rather than returning a sentinel value on division by zero; this should be verified directly in the full repository (e.g., via a Devin session) before finalizing severity, since Clarity's native `/` operator does abort on division by zero unless the helper explicitly guards against it.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L350-361)
```text
(define-private (calc-treasury-lp-preview)
  (let ((scaled-principal (var-get principal-scaled))
        (idx (var-get index))
        (next (next-index))
        (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
        (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
        (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
        (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
        (ta-preview (total-assets-preview)))
    (if (> reserve-inc u0)
        (mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))
        u0)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L763-770)
```text
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L797-806)
```text
(define-public (redeem (amount uint) (min-out uint) (recipient principal))
  (let (
    (states (var-get pause-states))
    (u (try! (accrue)))
    (account contract-caller)
    (current-assets (var-get assets))
    (balance (get-balance-internal account))
    (balance-check (asserts! (>= balance amount) ERR-INSUFFICIENT-BALANCE))
    (available-assets (get-available-assets))
    (inkind (convert-to-assets-preview amount)))
```
