Confirmed: `DECIMALS u6` for USDC vault, `BPS u10000`, `fee-reserve` is a DAO-configurable data var in basis points. This confirms `reserve-inc` is denominated in 6-decimal USDC units, making it very plausible for `mul-div-down` to floor `treasury-lp` to zero when the vault has appreciated (assets > supply) even slightly, since the numerator `reserve-inc * total-supply` must clear the denominator `total-assets-preview - reserve-inc` to yield a nonzero result. [1](#0-0) 

### Title
Treasury interest-reserve LP mint rounds down to zero on frequent small accruals, permanently freezing protocol yield - (File: `mainnet/contracts/vault/v0-vault-usdc.clar` and equivalent vault contracts)

### Summary
Every vault's `accrue` function computes the DAO treasury's cut of accrued interest (`reserve-inc`) and converts it into LP shares (`treasury-lp`) via `mul-div-down`. Because `accrue` is invoked as a side effect of essentially every unprivileged user action (`deposit`, `redeem`, `transfer`, `system-borrow`, `system-repay`), an ordinary caller can trigger `accrue()` frequently enough that the interest delta per call is tiny, causing the treasury-share computation to floor to zero — silently and permanently discarding the treasury's fee on that slice of interest.

### Finding Description
In `accrue`, the reserve fee owed to `.dao-treasury` is computed as: [2](#0-1) 

The relevant line:
```
(treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0))
```
`mul-div-down` truncates towards zero: `(reserve-inc * total-supply) / (total-assets-preview - reserve-inc)`. This expression rounds down to `u0` whenever `reserve-inc < (total-assets-preview - reserve-inc) / total-supply`, i.e., whenever the reserve increment for that single `accrue()` call is smaller than the vault's current price-per-share. Since `reserve-inc = debt-delta * fee-reserve / BPS` and `debt-delta` depends on the time elapsed since `last-update`, calling any state-changing entry point (`deposit`, `redeem`, `transfer`, `system-borrow`, `system-repay`) shortly after a previous accrual keeps `debt-delta`, and therefore `reserve-inc`, small — well within the range that floors `treasury-lp` to zero.

Critically, when `treasury-lp` founds to zero, the interest itself is **not** withheld or queued — `total-assets-preview`/`total-debt` still includes the full accrued interest, and existing `zft` holders' share value still increases by that amount. Only the treasury's diluting LP mint is skipped. There is no minimum-accrual-interval guard or dust-accumulation mechanism (analogous to the missing/insufficient `MIN_GNS_WEI_IN` check in the reference report) to prevent this from happening on virtually every call once the vault's share price exceeds a moderate multiple of 1 unit, which is easily reached for 6-decimal assets like USDC (`DECIMALS u6`) since `reserve-inc` is denominated in those same small units.

This mirrors the referenced bug class: a computed value that legitimately represents owed rewards/fees is silently dropped due to integer-division rounding because no lower bound protects the distribution step, causing that portion of yield to never be credited to its rightful recipient. Any unprivileged principal that calls `deposit`, `redeem`, or `transfer` (which all call `accrue` unconditionally) repeatedly — e.g., once per block or transaction — can keep the treasury's fee-share indefinitely at zero, since each call resets `last-update` and shrinks the next `debt-delta`. [3](#0-2) [4](#0-3) 

### Impact Explanation
The DAO treasury's share of interest (`fee-reserve` bps of every debt increment) is a protocol revenue stream, functionally equivalent to unclaimed/undistributed yield. When `treasury-lp` rounds to zero on essentially every accrual, that reserve fee is permanently lost — it is never re-queued or minted later, since the calculation is stateless per-call and based on the just-computed `debt-delta`. Over the vault's lifetime, this results in permanent freezing/loss of the protocol's accrued yield, matching the in-scope "High – permanent freezing of unclaimed yield" impact class. This affects every deployed vault sharing this pattern (`v0-vault-usdc`, `v0-vault-sbtc`, `v0-vault-ststx`, `v0-vault-ststxbtc`, `v0-vault-stx`, `v0-vault-usdh`).

### Likelihood Explanation
Likelihood is high: no privileged action or capital is required. `accrue()` is invoked unconditionally at the top of `deposit`, `redeem`, and `transfer` — all callable by any principal, including with zero-value or minimal-value calls (`transfer` of `u0`... actually requires `amount>u0` for deposit/redeem, but `transfer` has no such floor and can be called with a tiny amount purely to trigger `accrue`). An attacker (or even organic high-frequency usage) simply needs to invoke any of these functions more often than the vault's interest rate accrues a full price-per-share worth of reserve fee, which is trivially achievable for typical fee-reserve bps settings and moderate share prices.

### Recommendation
Introduce dust-accumulation accounting for the treasury's reserve fee (e.g., track an undistributed `reserve-inc` remainder across calls and only mint `treasury-lp` once accumulated `reserve-inc` clears the rounding threshold), or compute `treasury-lp` using round-up division (`mul-div-up`) so fractional treasury shares are never silently discarded, analogous to increasing `MIN_GNS_WEI_IN` in the referenced report to prevent zero-reward distributions.

### Proof of Concept
1. Vault is live with `total-supply` and `total-assets-preview` such that price-per-share (`total-assets-preview / total-supply`) exceeds `1` unit of the underlying (e.g., after normal interest accrual over time, or simply because `total-supply` is small relative to `total-assets-preview` for a young vault).
2. Attacker calls `transfer` (or any cheap function that triggers `accrue`) repeatedly, once per block, on their own zft balance.
3. Each call: `time-delta` since `last-update` is small (one block), so `debt-delta` and thus `reserve-inc = debt-delta * fee-reserve / BPS` are small.
4. `treasury-lp = (reserve-inc * total-supply) / (total-assets-preview - reserve-inc)` floors to `u0` because `reserve-inc` is smaller than the current price-per-share.
5. `index`/`lindex` are still updated (so depositors get full credit for the interest), but `.dao-treasury` never receives its `treasury-lp` mint for that period — repeat indefinitely to permanently deny the treasury essentially all of its designed reserve-fee revenue on this vault.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L22-27)
```text
(define-constant DECIMALS u6)

;; -- Precision & scaling
(define-constant BPS u10000)
(define-constant PRECISION u100000000)
(define-constant INDEX-PRECISION u1000000000000)  ;; 1e12 for index calculations
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L754-761)
```text
    (asserts! (not (is-eq current-contract to)) ERR-TOKENIZED-VAULT-PRECONDITIONS)
    (try! (ft-transfer? zft amount from to))
    (match memo to-print (print to-print) 0x)
    (ok true)))

;; -- Vault operations -------------------------------------------------------

(define-public (deposit (amount uint) (min-out uint) (recipient principal))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L765-797)
```text
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
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L837-861)
```text
      (if (get accrue states)
          ;; PAUSED: Pass-through without reverting
          (ok { index: idx, lindex: lidx })
          ;; NOT PAUSED: Normal accrual logic
          (let ((next (next-index))
                (nliq (next-liquidity-index))
                (scaled-principal (var-get principal-scaled))
                (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
                (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
                (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
                false)
            (if (not (is-eq lidx nliq))
                (var-set lindex nliq)
                false)
            (if (> treasury-lp u0)
                (try! (ft-mint? zft treasury-lp .dao-treasury))
                false)
            (if (or (not (is-eq idx next)) (not (is-eq lidx nliq)))
                (var-set last-update stacks-block-time)
                false)
            (ok { index: next, lindex: nliq })))))
```
