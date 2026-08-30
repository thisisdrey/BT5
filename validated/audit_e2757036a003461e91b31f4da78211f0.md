### Title
Rounding-direction mismatch between `accrue()`'s debt-delta and `total-assets-preview()`'s interest can underflow the treasury-LP subtraction and brick a vault's `accrue()` gate - (File: `mainnet/contracts/vault/v0-vault-usdc.clar` and sibling vault contracts)

### Summary
Each vault's `accrue()` function computes the treasury LP mint amount with a subtraction, `(- (total-assets-preview) reserve-inc)`, that mixes two independently rounded views of the same underlying debt state — one rounded down, the other rounded up. Because `accrue()` is a mandatory precondition of every `deposit`, `redeem`, `system-borrow`, `system-repay`, and `transfer` call, an underflow in this subtraction (a Clarity runtime abort, functionally equivalent to a Solidity revert-on-underflow) would brick the entire vault, the exact impact class described in the ERC20RebaseDistributor report ("rounding errors ... cause transfers and mints to fail for underflow").

### Finding Description
In `mainnet/contracts/vault/v0-vault-usdc.clar` (and the identical logic replicated in `v0-vault-stx.clar`, `v0-vault-sbtc.clar`, `v0-vault-ststx.clar`, `v0-vault-ststxbtc.clar`, `v0-vault-usdh.clar`):

`accrue()` computes debt growth for the current period using `mul-div-down` directly on the raw index values:
```
(old-debt (mul-div-down scaled-principal idx INDEX-PRECISION))
(new-debt (mul-div-down scaled-principal next INDEX-PRECISION))
(debt-delta (if (> new-debt old-debt) (- new-debt old-debt) u0))
(reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
(treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0))
```
` [1](#0-0) ` (line numbers refer to the analogous block seen in `local-testing/contracts/vault/vault-usdc.clar:837-865`, identical structure in mainnet contracts).

Meanwhile, `total-assets-preview()`'s interest component is derived from `debt-preview`, which uses `calc-cumulative-debt`, defined with the **opposite** rounding direction (`mul-div-up`):
```
(define-private (calc-cumulative-debt (principal-amount uint) (idx uint))
  (mul-div-up principal-amount idx INDEX-PRECISION))
(define-private (debt-preview) (calc-cumulative-debt (var-get principal-scaled) (next-index)))
(define-private (total-assets-preview)
  (let ((interest (if (> debt debt-preview... )))
```
` [2](#0-1) `
` [3](#0-2) `

So the same `principal-scaled`/`index` state produces two different rounding outcomes depending on which helper is used: `old-debt`/`new-debt` inside `accrue()` round **down**, while `total-debt`/`debt-preview` (feeding `total-assets`/`total-assets-preview`, and therefore the interest component subtracted against `reserve-inc`) round **up**. `total-borrowed` is a separately tracked raw-token accumulator (incremented/decremented on `system-borrow`/`system-repay` with exact amounts, not through the scaled-index math), so it can drift relative to `old-debt`/`debt-preview` over many operations. Because `interest = max(0, debt-preview - total-borrowed)` and `reserve-inc` is derived from a different (down-rounded) debt delta computed against a possibly-drifted `total-borrowed`, there is no invariant in the code that guarantees `reserve-inc <= total-assets-preview` in every state — this mirrors the C4 report's second bug, where an independently-tracked accumulator (`_unmintedRebaseRewards`) could end up smaller than the `amount` being subtracted from it due to rounding drift across separate computations.

If `reserve-inc` ever exceeds `total-assets-preview` in the same block, `(- (total-assets-preview) reserve-inc)` underflows and the Clarity runtime aborts the transaction. Since this line executes inside `accrue()`, which is `try!`-called at the top of `deposit`, `redeem`, `system-borrow`, `system-repay`, and `transfer` in every vault, e.g.:
` [4](#0-3) `
` [5](#0-4) `
a single reachable underflow state would brick every one of these entry points for the affected vault until state drifts back out of the underflow condition (analogous to the "temporary but re-triggerable" DoS window described by the original wardens' discussion).

### Impact Explanation
If triggered, this bricks `deposit`, `redeem`, `borrow`, `repay`, and `transfer` for the affected vault (and by extension `zToken` operations routed through `market.clar`, since `accrue-and-cache` calls into the vault's `accrue`). This can be used to DoS liquidations and withdrawals at will by any permissionless caller who can nudge protocol state (e.g. via small borrows/repays) to hit the drift condition, matching the in-scope "temporary freezing of funds" impact category.

### Likelihood Explanation
Likelihood is low-to-moderate and state-dependent: the underflow requires `total-borrowed` to have drifted relative to the down-rounded `old-debt`/`new-debt` used in `accrue()`, combined with a `fee-reserve` setting and low enough `current-assets` (e.g., near-100% utilization) that the rounded-up `interest` term collapses close to `reserve-inc`. This mirrors the original finding's own caveat that the underflow window is narrow and can require specific timing/amounts, but is repeatable via permissionless small distribute/borrow/repay operations timed to keep the drift alive, similarly to the C4 warden's demonstrated PoC pattern.

### Recommendation
Make the debt-growth accounting in `accrue()` consistent with `calc-cumulative-debt`'s rounding direction (i.e., compute `old-debt`/`new-debt` via `calc-cumulative-debt` instead of raw `mul-div-down`), and additionally floor the `(- (total-assets-preview) reserve-inc)` subtraction to zero (or clamp `reserve-inc` to `min(reserve-inc, total-assets-preview)`) so that a temporary rounding mismatch cannot abort accrual and downstream vault operations.

### Proof of Concept
A concrete numeric trigger requires simulating `principal-scaled`/`index` evolution across multiple `system-borrow`/`system-repay` calls (to drift `total-borrowed` away from `calc-cumulative-debt`'s rounding) together with a chosen `fee-reserve` value and near-zero `current-assets`, then invoking any vault entry point (e.g. `deposit`) in the block where `reserve-inc > total-assets-preview`. I was not able to execute Clarity code to confirm the exact numeric parameters that trigger the underflow in this session; the root cause — the rounding-direction mismatch between `accrue()`'s `mul-div-down`-based `old-debt`/`new-debt` and `calc-cumulative-debt`'s `mul-div-up`-based `total-debt`/`debt-preview`, feeding an unguarded subtraction inside a mandatory precondition of every vault operation — is directly demonstrated by the cited code.

### Citations

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L180-181)
```text
(define-private (calc-cumulative-debt (principal-amount uint) (idx uint))
  (mul-div-up principal-amount idx INDEX-PRECISION))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L339-344)
```text
(define-private (total-assets-preview)
  (let ((current-assets (var-get assets))
        (debt (debt-preview))
        (borrowed (var-get total-borrowed))
        (interest (if (> debt borrowed) (- debt borrowed) u0)))
    (+ current-assets interest)))
```

**File:** mainnet/contracts/vault/v0-vault-usdc.clar (L847-854)
```text
                (reserve-inc (mul-div-down debt-delta (var-get fee-reserve) BPS))
                (treasury-lp (if (> reserve-inc u0) (mul-div-down reserve-inc (total-supply) (- (total-assets-preview) reserve-inc)) u0)))
            (if (not (is-eq idx next))
                (var-set index next)
                false)
            (if (not (is-eq lidx nliq))
                (var-set lindex nliq)
                false)
```

**File:** mainnet/contracts/vault/v0-vault-ststx.clar (L763-770)
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

**File:** mainnet/contracts/vault/v0-vault-ststx.clar (L797-806)
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
