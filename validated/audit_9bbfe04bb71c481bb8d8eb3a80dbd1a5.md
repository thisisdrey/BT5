### Title
`accrue()` unguarded division/subtraction in treasury-LP minting can revert and permanently DoS all vault operations - (File: `mainnet/contracts/vault/v0-vault-stx.clar`, replicated across all `v0-vault-*.clar` contracts)

### Summary
The `sharesToTokenAmount`-style analog in Zest's vault contracts is the treasury-LP share calculation performed inside `accrue()` (and its preview twin `calc-treasury-lp-preview`). Both compute `mul-div-down reserve-inc (total-supply) (- ta-preview reserve-inc))` without ever checking whether the denominator `(- ta-preview reserve-inc)` is zero (or would underflow), exactly the same unguarded-denominator pattern flagged in the referenced Biconomy finding.

### Finding Description
`accrue()` computes the DAO-treasury LP mint amount as: [1](#0-0) 
where `treasury-lp` divides `reserve-inc * total-supply` by `(- (total-assets-preview) reserve-inc)`. This subtraction/division is not protected by any zero (or underflow) check, unlike the sibling helpers `convert-to-shares-preview`/`convert-to-assets-preview` in the same file, which explicitly guard `is-eq ts u0` and `is-eq ta u0` before dividing: [2](#0-1) 

The read-only preview version has the identical unguarded pattern: [3](#0-2) 

If `total-assets-preview()` is ever equal to or less than `reserve-inc` at the moment `accrue()` runs, Clarity's uint subtraction underflows (traps) or, in the edge case of exact equality, the division is by zero — either way the transaction reverts. Because `accrue()` is called unconditionally as the first step of essentially every state-changing vault entry point — `deposit`, `redeem`, `system-borrow`, `system-repay`, and `set-*` config setters — a state where this denominator collapses to zero makes the entire vault unusable: deposits, withdrawals and repayments all revert. [4](#0-3) 

This same code (byte-for-byte) is duplicated in every vault: `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, so the flaw is protocol-wide, not vault-specific. [5](#0-4) 

### Impact Explanation
Once `total-assets-preview() <= reserve-inc` for a vault, every call to `accrue()` reverts, which cascades into `deposit`, `redeem`, `system-borrow`, and `system-repay` all reverting on that vault. This is a protocol-wide, self-inflicted denial of service that freezes user deposits/withdrawals in that vault indefinitely (until DAO intervention, if even possible, since even config setters like `set-fee-reserve`/`set-points-util` call `accrue()` first and would also revert). This matches the in-scope "temporary freezing of funds" impact class; if it becomes permanently unrecoverable (no path to reset `total-borrowed`/`assets`/`principal-scaled` without going through `accrue()`), it could escalate to permanent freezing/insolvency-adjacent DoS.

### Likelihood Explanation
The precondition (`total-assets-preview() <= reserve-inc`) requires the vault's current liquid `assets` to be reduced to near zero (heavily utilized/borrowed) at a moment where accrued interest since the last `accrue()` call is small relative to the fee-reserve cut of the *incremental* debt delta. This is a tight numerical edge case under normal parameters (`fee-reserve < BPS` is enforced by `set-fee-reserve`), but the underflow/zero-denominator path is entirely unguarded — no invariant in the code proves it is unreachable, and an attacker who can influence deposit/withdraw/borrow/repay timing and amounts (all callable by ordinary principals or their own contracts) can attempt to drive `assets` toward zero while shaping successive `accrue()` calls to minimize the interest window, pushing `ta-preview` arbitrarily close to `reserve-inc`. Given the missing check is identical in every deployed vault, and the entry points are fully public, this is a credible, no-privilege-required DoS vector.

### Recommendation
Guard the treasury-LP computation the same way `convert-to-shares-preview`/`convert-to-assets-preview` are guarded: check `(> ta-preview reserve-inc)` before computing `(- ta-preview reserve-inc)`, and short-circuit to `u0` treasury-lp mint (or skip the mint and simply defer the reserve fee) whenever the denominator would be zero or negative. Apply this fix identically to `calc-treasury-lp-preview` and the inline `treasury-lp` computation in `accrue()` in every `v0-vault-*.clar` contract.

### Proof of Concept
1. Deploy/observe a vault where sustained heavy borrowing drives `get-available-assets` (and thus `assets`) toward `u0`, while `total-borrowed` and `principal-scaled` remain large.
2. Wait for/trigger a state where `total-assets-preview()` (current-assets + accrued interest since last update) becomes numerically close to `reserve-inc` (fee-reserve share of the new debt delta since last `accrue()`); this is more easily reached with a DAO-configured `fee-reserve` near the `BPS-1` ceiling combined with a near-zero `assets` balance and a short interval since the last accrual.
3. Call any public function that begins with `(try! (accrue))`, e.g. `deposit` or `redeem` with any valid amount.
4. `accrue()` executes `(mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc))`; when `(total-assets-preview)` <= `reserve-inc`, the subtraction underflows/the division denominator is zero, and the transaction reverts, blocking the caller (and everyone else, since every entry function calls `accrue()` first).

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L308-324)
```text
(define-private (convert-to-shares-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ts u0)
        amount
        (if (is-eq ta u0)
            u0
            (mul-div-down amount ts ta)))))

(define-private (convert-to-assets-preview (amount uint))
  (let ((ta (total-assets-preview))
        (ts (total-supply-preview)))
    (if (is-eq ta u0)
        u0
        (if (is-eq ts u0)
            u0
            (mul-div-down amount ta ts)))))
```

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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L763-817)
```text
(define-public (deposit (amount uint) (min-out uint) (recipient principal))
    (let (
      (states (var-get pause-states))
      (u (try! (accrue)))
      (account contract-caller)
      (CAP-SUPPLY (var-get cap-supply))
      (current-assets (var-get assets))
      (inkind (convert-to-shares-preview amount)))

    (asserts! (not (get deposit states)) ERR-PAUSED)
    (asserts! (var-get initialized) ERR-INIT)
    (asserts! (not (var-get in-flashloan)) ERR-REENTRANCY)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (>= inkind min-out) ERR-SLIPPAGE)
    (asserts! (<= (+ current-assets amount) CAP-SUPPLY) ERR-SUPPLY-CAP-EXCEEDED)

    (try! (receive-underlying amount account))
    (try! (ft-mint? zft inkind recipient))
    (var-set assets (+ current-assets amount))
    
    (print {
      action: "deposit",
      caller: contract-caller,
      data: {
        depositor: account,
        recipient: recipient,
        amount: amount,
        shares-minted: inkind,
        assets: (+ current-assets amount)
      }
    })

    (ok inkind)))

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

  (asserts! (>= current-assets inkind) ERR-INSUFFICIENT-ASSETS)
  (asserts! (not (get redeem states)) ERR-PAUSED)
  (asserts! (> amount u0) ERR-AMOUNT-ZERO)
  (asserts! (> inkind u0) ERR-OUTPUT-ZERO)
  (asserts! (>= inkind min-out) ERR-SLIPPAGE)
  (asserts! (>= available-assets inkind) ERR-INSUFFICIENT-LIQUIDITY)

  (try! (ft-burn? zft amount account))
  (try! (send-underlying inkind recipient))
  (var-set assets (- current-assets inkind))
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L843-850)
```text
          (let ((next (next-index))
                (nliq (next-liquidity-index))
                (scaled-principal (var-get principal-scaled))
                (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L336-360)
```text
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))

(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))

;; -- Treasury LP preview helpers --------------------------------------------

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
