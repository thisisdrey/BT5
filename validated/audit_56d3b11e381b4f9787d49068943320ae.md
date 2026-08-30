### Title
Deposit/redeem in the vault contracts can become permanently uncallable after a large loss skews the shares-to-assets ratio, due to unguarded `mul-div-down`/`mul-div-up` multiplication overflow - (File: `mainnet/contracts/vault/v0-vault-stx.clar` and equivalent per-asset vault contracts)

### Summary
The StakeWise report shows `enterExitQueue()` becoming uncallable because a `uint96` downcast overflows once the shares-to-assets ratio becomes extreme after a large loss. Clarity has no fixed-width downcast, but it does have a fixed maximum `uint` (`2^128-1`) and Clarity's `*` operator aborts the transaction if the product exceeds that bound. The same "extreme shares-to-assets ratio after a large loss" precondition can drive `convert-to-shares-preview`/`convert-to-assets-preview` multiplications in Zest's vaults past `MAX-U128`, causing `deposit()`/`redeem()` to revert unconditionally for as long as the skewed ratio persists.

### Finding Description
Each per-asset vault (`v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-usdc.clar`, `v0-vault-usdh.clar`, `v0-vault-ststxbtc.clar`) defines share/asset conversion using an unchecked multiply-then-divide helper: [1](#0-0) 

which is used directly in `convert-to-shares-preview` / `convert-to-assets-preview`: [2](#0-1) 

`deposit()` calls `convert-to-shares-preview(amount)` = `amount * total-supply / total-assets`, and `redeem()` calls `convert-to-assets-preview(amount)` = `amount * total-assets / total-supply`: [3](#0-2) 

The vault also exposes `socialize-debt`, which can slash `assets` (and thus `total-assets`) by an arbitrary amount while leaving `total-supply` (outstanding zft shares) unchanged, exactly the "huge loss" precondition described in the report: [4](#0-3) 

Once `total-assets` is driven very low relative to `total-supply`, `total-supply/total-assets` becomes a very large number. Clarity's native `uint` is capped at `MAX-U128` (`u340282366920938463463374607431768211455`): [5](#0-4) 

and Clarity aborts a transaction if `*` overflows this bound. Any subsequent `deposit()` call computing `amount * total-supply` (before dividing by the now-tiny `total-assets`) can therefore overflow and abort, making `deposit()` unconditionally revert for ordinary users as long as the skewed ratio persists — the direct analog of `enterExitQueue()` becoming uncallable in the StakeWise report.

### Impact Explanation
If `deposit()` becomes permanently uncallable on a vault, no new liquidity can enter that vault, and by extension the corresponding market-side borrow/repay/liquidation flows that depend on that vault's liquidity are degraded. Because `total-assets` and `total-supply` do not self-correct (there is no re-basing or ratio reset mechanism visible in the reviewed conversion helpers), this is a **temporary/permanent freezing of funds** for that vault's deposit path, landing in the in-scope "temporary freezing of funds" impact class.

### Likelihood Explanation
Reaching the trigger condition requires `total-assets` to be reduced to a very small value while `total-supply` remains comparatively large. `socialize-debt` is the mechanism shown here that can produce this state; whether it is reachable by an ordinary caller or restricted to a privileged/system caller could not be fully confirmed — the guard is `(try! (check-caller-auth))`, and I was unable to retrieve the body of `check-caller-auth` before running out of iterations, so I cannot confirm the exact caller-permission model. [6](#0-5) 

If `check-caller-auth` restricts the call to the market contract acting on a legitimate bad-debt event from liquidations (a normal, unprivileged code path such as cascading undercollateralized liquidations), likelihood is moderate. If it is DAO/admin-gated only, this finding would fall under the excluded "requires DAO compromise" category per the rules, and should be treated as **unproven** until that check is verified.

### Recommendation
- Confirm the exact permission model of `check-caller-auth` and whether `socialize-debt` (or any other path reducing `assets` disproportionately to `total-supply`) is reachable through ordinary liquidation flows without DAO/admin involvement.
- Add explicit bounds checking or a safe-multiply helper (checking `x <= MAX-U128 / y` before multiplying) in `mul-div-down`/`mul-div-up`, or clamp/normalize the shares-to-assets ratio so that `convert-to-shares-preview`/`convert-to-assets-preview` cannot be driven into an overflow-prone regime.
- Consider a minimum-total-assets floor (analogous to `MINIMUM-LIQUIDITY`) enforced after any loss-socialization event, or a vault-pause/ratio-reset mechanism triggered automatically when the shares-to-assets ratio crosses a dangerous threshold.

### Proof of Concept
Conceptual sequence (exact reachability of step 1 by an unprivileged principal is unconfirmed):
1. A large bad-debt event occurs on a vault (e.g., through liquidation shortfall) and the entity permitted by `check-caller-auth` calls `socialize-debt` with a large `scaled-amount`, driving `assets` (and thus `total-assets`) down to a very small value while `total-supply` (outstanding zft) remains large — see `socialize-debt` at [7](#0-6) .
2. Any ordinary user calls `deposit(amount, min-out, recipient)` with a normal `amount`. Internally this evaluates `convert-to-shares-preview(amount)` = `mul-div-down(amount, total-supply, total-assets)` = `(amount * total-supply) / total-assets` — see [8](#0-7)  and [9](#0-8) .
3. Because `total-supply` is now disproportionately large relative to `total-assets`, `amount * total-supply` can exceed `MAX-U128` (`u340282366920938463463374607431768211455`, [5](#0-4) ), causing Clarity's runtime to abort the multiplication and revert the `deposit()` call for any typical deposit amount, permanently freezing new deposits into the vault until the ratio is otherwise corrected.

### Citations

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L31-31)
```text
(define-constant MAX-U128 u340282366920938463463374607431768211455)
```

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L147-151)
```text
(define-private (mul-div-down (x uint) (y uint) (z uint))
  (/ (* x y) z))

(define-private (mul-div-up (x uint) (y uint) (z uint))
  (/ (+ (* x y) (- z u1)) z))
```

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

**File:** mainnet/contracts/vault/v0-vault-stx.clar (L944-970)
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

    (try! (check-caller-auth))
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (var-set lindex new-lindex)
    (var-set principal-scaled (if (> scaled-principal scaled-amount) (- scaled-principal scaled-amount) u0))
    (var-set total-borrowed (if (> borrowed principal-reduction) (- borrowed principal-reduction) u0))
    (var-set assets (if (> current-assets principal-reduction) (- current-assets principal-reduction) u0))

    (print {
      action: "socialize-debt",
      caller: contract-caller,
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L761-808)
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
```
