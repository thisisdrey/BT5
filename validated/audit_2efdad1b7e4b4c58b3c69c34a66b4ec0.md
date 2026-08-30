## Analog Found

### Title
Rounding-direction mismatch between aggregate collateral valuation and single-asset removal valuation causes legitimate `collateral-remove` calls to revert (DoS) - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`v0-4-market.clar`'s `collateral-remove` computes the USD value of a position's aggregate collateral by rounding **down** per-asset, but computes the USD value of the specific amount being removed by rounding **up**. When these two independently-rounded values represent conceptually the same quantity (e.g. removing 100% of one collateral asset while another held collateral contributes zero rounded value), the removal value can exceed the aggregate value by exactly the rounding delta, causing the safety `asserts!` to revert a transaction that should succeed — the same root cause as the Rubicon `M-1` finding (two independent computations of the same amount, rounded in opposite directions, then compared with a strict inequality).

### Finding Description
In `get-notional-evaluation` → `calculate-asset-notional-value`, each collateral asset's USD contribution is computed with round-**down**: [1](#0-0) 

while `collateral-remove` computes the USD value of the specific amount being withdrawn with round-**up**, via `find-and-resolve-asset-value(..., round-up=true)`: [2](#0-1) [3](#0-2) 

Both ultimately call the same `normalize` helper, only differing by the `round-up` flag: [4](#0-3) 

The enabled-collateral branch then guards against underflow with a strict comparison between these two independently-rounded quantities: [5](#0-4) 

Let `curr-coll-aid` be the user's full balance for the asset being (fully) removed, and `amount = curr-coll-aid` (a 100% withdrawal of that asset). The asset's contribution to `collateral-value` is `X_down = normalize(curr-coll-aid * price, decimals, false)`. The `removed-asset-value` for the same `amount` is `X_up = normalize(curr-coll-aid * price, decimals, true)`, which equals `X_down` or `X_down + 1` depending on whether `curr-coll-aid * price` is an exact multiple of `10^decimals`. The check requires:

```
collateral-value >= removed-asset-value
(sum_of_other_collateral + X_down) >= X_up
```

If `sum_of_other_collateral == 0` (no other enabled collateral) or rounds to `0` (e.g. a dust amount of a second enabled collateral asset whose USD-normalized value truncates to `0`), and `X_up = X_down + 1` (the common case whenever the raw product isn't an exact multiple of the decimal factor), the inequality fails and the call reverts with `ERR-INSUFFICIENT-COLLATERAL` even though the user is removing exactly the collateral they hold and the position would be perfectly solvent.

This is functionally identical to the Rubicon bug: `RubiconFeeController` rounded a fee up while `ProtocolFees` validated it by rounding the same conceptual quantity down, so a legitimate transaction reverted due to a 1-unit rounding delta between two independent computations of the same amount.

### Impact Explanation
This causes a **temporary freezing of funds** — an ordinary borrower attempting to fully withdraw one collateral asset (e.g., to rebalance collateral or exit a position for a specific asset while retaining other collateral/debt) can have the transaction unconditionally revert due to the rounding mismatch, even though the operation is otherwise safe and healthy. This blocks normal collateral management for affected users until they add extra collateral cushion or otherwise work around the bug, matching the in-scope "temporary freezing of funds" impact class.

### Likelihood Explanation
The trigger condition — removing 100% of one enabled collateral asset while the position's other collateral contributes a rounded value of `0` (or no other collateral exists in a scenario where zero residual debt is expected) — is a normal, foreseeable usage pattern (collateral rebalancing, exiting one collateral type). The rounding mismatch itself (`X_up = X_down + 1`) occurs whenever `curr-coll-aid * price` is not an exact multiple of `10^decimals`, which is the common case for arbitrary price/amount combinations, not a rare edge case.

### Recommendation
Use consistent rounding for the same conceptual quantity across both computations: either round the per-asset aggregate `collateral-value` up for the asset being removed (or compute the post-removal collateral value directly from the same rounding basis as the total, rather than subtracting an independently-rounded partial value), or cap `removed-asset-value` with `min(removed-asset-value, collateral-value)`-style saturation instead of an unconditional `asserts!` that can fail purely due to rounding.

### Proof of Concept
1. User deposits collateral asset A (e.g. sBTC) as their sole collateral or with a dust amount of a second enabled collateral asset B.
2. User borrows against A/B in asset C (`debt-value > 0`), keeping the position healthy.
3. User calls `collateral-remove` with `amount = curr-coll-aid` (100% of asset A), intending to swap collateral or close out that leg while retaining B and the debt.
4. Inside `collateral-remove`: `disabled`/enabled branch computes `collateral-value` using round-down for A's full balance, but `removed-asset-value` for the same `amount` using round-up.
5. If `curr-coll-aid * price` is not an exact multiple of `10^decimals`, and B's rounded contribution is `0`, `collateral-value < removed-asset-value` by exactly `1`, and the `(asserts! (>= collateral-value removed-asset-value) ERR-INSUFFICIENT-COLLATERAL)` check at [6](#0-5)  reverts a transaction that should succeed.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L556-561)
```text
        (collateral-list (get clist acc))
        (debt-list (get dlist acc))
        (coll-amount (find-collateral-amount collateral-list asset-id))
        (coll-notional (if (> coll-amount u0)
                           (normalize (* coll-amount price) decimals false)
                           u0))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L576-580)
```text
(define-private (normalize (value uint) (decimals uint) (round-up bool))
  (let ((decimal-factor (pow u10 decimals)))
    (if round-up
      (div-up value decimal-factor)
      (div-down value decimal-factor))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L668-676)
```text
(define-private (find-and-resolve-asset-value
                  (assets (list 64 
                    { id: uint, addr: principal, decimals: uint,
                    oracle: { type: (buff 1), ident: (buff 32), callcode: (optional (buff 1)), max-staleness: uint },
                    collateral: bool, debt: bool, price: uint }))
                  (asset-id uint) (amount uint) (round-up bool))
  (match (find-asset asset-id assets)
    asset (normalize (* amount (get price asset)) (get decimals asset) round-up)
    u0))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1128-1134)
```text
              (removing-all (is-eq amount curr-coll-aid))
              (current-group (try! (get-egroup position-mask)))
              (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))
              (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
              (collateral-value (get collateral notional-valued-assets))
              (debt-value (get debt notional-valued-assets))
              (removed-asset-value (find-and-resolve-asset-value assets asset-id amount true)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1136-1140)
```text
          (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
          (asserts!
            (if is-collateral-enabled
                (let ((t (asserts! (>= collateral-value removed-asset-value) ERR-INSUFFICIENT-COLLATERAL))
                      (post-removal-collateral-value (- collateral-value removed-asset-value)))
```
