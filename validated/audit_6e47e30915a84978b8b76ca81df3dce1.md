### Title
Protocol's `fee-reserve` (treasury LP) accrual rounds to zero for low-decimal vaults (USDC/USDH) due to integer-division precision loss and checkpoint-based accrual - ([File: mainnet/contracts/vault/v0-vault-usdc.clar])

### Summary
The `accrue()` function in the Zest vault contracts computes the protocol's fee cut (`reserve-inc`, minted as treasury LP shares) from `debt-delta`, the raw interest accrued in the underlying asset's native decimals. For 6-decimal assets like USDC, `debt-delta` per `accrue()` call can be a very small integer, and dividing it by `BPS` (10000) via `mul-div-down` rounds down to zero far more easily than it would for 18-decimal assets. Because each `accrue()` call is a checkpoint (advancing `index`/`lindex`/`last-update`), any interest/fee amount that rounds to zero within that call window is permanently lost and cannot be recovered on the next call.

### Finding Description
`accrue()` in `mainnet/contracts/vault/v0-vault-usdc.clar` computes: [1](#0-0) 

```
(let ((next (next-index))
      (nliq (next-liquidity-index))
      (scaled-principal (var-get principal-scaled))
      (old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
      (new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
      (debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
      (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
      (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
  ...
  (if (or (not (is-eq idx next)) (not (is-eq lidx nliq)))
      (var-set last-update stacks-block-time)
      false)
  ...)
``` [2](#0-1) 

`DECIMALS` for this vault is `u6` (USDC), while `BPS` is `u10000` and `INDEX-PRECISION` is `u1000000000000` (1e12) - the index math itself operates at 1e12 precision, but `debt-delta` (and thus `reserve-inc`) is expressed in the underlying token's *native* decimals (6 for USDC), not scaled to 18 decimals. This is the exact analog of the reported bug: `debt-delta` for USDC-denominated pools represents raw 6-decimal units, so for realistic small time deltas (frequent `accrue()` calls triggered on every `deposit`/`redeem`/`borrow`/`repay`) `debt-delta * fee-reserve / BPS` truncates to `0`, and the treasury's `reserve-inc`/`treasury-lp` mint is skipped (`(if (> reserve-inc u0) ... u0)`). Since `last-update` (and `index`) are checkpointed forward on every `accrue()` call regardless, the lost fee for that interval can never be recaptured.

Any unprivileged caller can trigger `accrue()` indirectly by calling `deposit`, `redeem`, `system-borrow`, or `repay` on the vault (all begin with `(u (try! (accrue)))`), so an attacker (or even organic high-frequency usage) can force frequent checkpointing with minimal capital, systematically starving the protocol's `fee-reserve` (treasury) revenue on USDC/USDH vaults.

### Impact Explanation
This causes permanent loss of the protocol's fee revenue (`treasury-lp` minted to `.dao-treasury`) - unclaimed yield that the protocol/DAO would otherwise be entitled to. Repeated triggering of `accrue()` at low cost (each call costs only network fees, no minimum-amount gate on plain `accrue`-triggering actions) makes this indefinitely repeatable, compounding to a large fraction of fees lost over time for the 6-decimal-asset vaults (USDC, USDH). This matches the High-impact category "theft of unclaimed yield or royalties" via loss to the treasury, since fees that should have accrued to the DAO treasury are permanently and irrecoverably zeroed out.

### Likelihood Explanation
Likelihood is high for the low-decimal vaults: the vulnerability doesn't require any privileged action or misconfiguration - it emerges naturally whenever `debt-delta` for a short interval, computed in 6-decimal units, is small enough to round `debt-delta * fee-reserve / BPS` to zero. This condition is easily and cheaply reproducible by any caller invoking `deposit`/`redeem`/`borrow`/`repay` (or by protocol usage patterns with frequent small transactions), and can be sustained indefinitely at low cost, similar to the original Sherlock finding accepted as Medium severity for Sentiment V2.

### Recommendation
Scale `debt-delta` (and the intermediate debt/interest computations) to a fixed high precision (e.g., 1e18) independent of the underlying asset's native decimals before applying the `fee-reserve`/`BPS` division, then scale back down to native decimals only for the final minted/transferred amount. Alternatively, accumulate fractional/truncated fee remainders across `accrue()` calls instead of discarding them at each checkpoint, so that precision loss cannot compound into total loss.

### Proof of Concept
Not independently executed in this analysis (no test harness run), but by direct analysis of the code:
1. For the USDC vault, `DECIMALS = u6`, `BPS = u10000`.
2. Suppose `fee-reserve` is set to `1000` (10%) and the vault has a modest `principal-scaled` corresponding to a few thousand USDC borrowed.
3. A caller calls `deposit`/`redeem`/`borrow`/`repay` shortly (seconds to low minutes) after the previous `accrue()` checkpoint. `debt-delta` (interest accrued in 6-decimal USDC units over that short interval) can easily be < 10 (raw units), such that `debt-delta * 1000 / 10000 = debt-delta / 10` rounds to `0` in integer division.
4. `reserve-inc` is `0`, so `treasury-lp` is `0` and no treasury LP is minted for that period `(if (> reserve-inc u0) ... u0)`.
5. Regardless, `last-update` advances to `stacks-block-time`, permanently discarding the ability to recover that period's fee.
6. Repeating steps 3-5 (triggerable by any account performing low-cost deposit/redeem calls at short intervals) causes the protocol's fee-reserve/treasury revenue on the USDC/USDH vaults to be systematically and cumulatively lost.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L19-27)
```text
(define-constant UNDERLYING 'SP120SBRBQJ00MCWS7TM5R8WJNTTKD5K0HFRC2CNE.usdcx)
(define-constant NAME "Zest USDC")
(define-constant SYMBOL "zUSDC")
(define-constant DECIMALS u6)

;; -- Precision & scaling
(define-constant BPS u10000)
(define-constant PRECISION u100000000)
(define-constant INDEX-PRECISION u1000000000000)  ;; 1e12 for index calculations
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L845-865)
```text
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

(define-public (system-borrow (amount uint) (receiver principal))
  (let (
      (states (var-get pause-states))
```
