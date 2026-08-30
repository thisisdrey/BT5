### Title
Unhealthy Position Locks Non-Recognized (Disabled) Collateral During `collateral-remove` - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`collateral-remove` in the market gates every withdrawal — including withdrawal of collateral assets that are no longer counted toward the position's health score — behind an unconditional check that the *current* recognized position is healthy. This mirrors the Euler EVC analog: a controller/health check fires on an action involving a token that is not recognized as valid collateral for the position, blocking the user from moving assets that have no bearing on the account's health.

### Finding Description
In `collateral-remove`, when the account has any debt, the function first computes `collateral-value`/`debt-value` strictly from the assets present in the user's current `position-mask` (`get-assets position-mask` → `get-notional-evaluation`), then enforces: [1](#0-0) 

```
(assets (get-assets position-mask))
...
(notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
(collateral-value (get collateral notional-valued-assets))
(debt-value (get debt notional-valued-assets))
...
(asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
```

This `is-healthy` gate runs unconditionally, before branching on whether the asset being withdrawn (`ft`/`asset-id`) is even `is-collateral-enabled`, i.e. before the code path that handles a *disabled* (non-recognized) collateral asset: [2](#0-1) 

For a disabled/non-recognized collateral asset, `collateral-value`/`debt-value` never include that asset's value (it is not part of `position-mask`'s egroup-derived `assets`), so removing it cannot change, improve, or worsen the account's LTV — it is economically inert with respect to the health check. Despite this, the earlier `(asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)` on line 1136 requires that the *recognized* portion of the position already be healthy, independent of the token being withdrawn. If the recognized collateral/debt puts the account below `LTV-BORROW`, the whole function reverts with `ERR-UNHEALTHY` even though the specific asset being withdrawn is not counted in that computation at all and its removal would not further jeopardize the position.

This is the direct analog of the Euler EVC issue: an account-status/health check fires on a token/vault interaction that is not recognized by the controlling risk logic (the mask/egroup), locking assets that have no relationship to the debt position.

### Impact Explanation
Users whose recognized collateral/debt position is unhealthy are unable to withdraw disabled/non-recognized collateral sitting in the same account, even though withdrawing it has zero effect on solvency. Since a user may eventually restore health (repay debt, add recognized collateral) and then withdraw, this is a **temporary freezing of funds** — an in-scope High impact per the rules.

### Likelihood Explanation
This triggers under ordinary, unprivileged usage: any account that (a) holds a once-enabled collateral asset later disabled by DAO governance (or otherwise not part of its current mask/egroup) and (b) has its recognized position dip below the borrow LTV threshold. No attacker action or DAO compromise is required — it is a reachable state during normal market operation and asset lifecycle changes.

### Recommendation
Restructure `collateral-remove` so the top-level `is-healthy` pre-check only gates withdrawals of assets that are actually counted in the position's recognized `collateral-value`/`debt-value` (i.e., assets in `position-mask`). For disabled/non-recognized collateral, the withdrawal should be permitted independent of the recognized position's current health, and only the disabled-branch's own accounting (which already isolates `disabled-notional`/`removal-notional`) should apply. Alternatively, document explicitly that disabled collateral becomes withdrawable only once the recognized position is healthy, mirroring Euler's documented "deposits may be withheld by an unhealthy controller" caveat.

### Proof of Concept
1. DAO enables asset `X` as collateral; Alice deposits `X` as collateral and borrows against other recognized collateral, remaining healthy.
2. DAO later disables `X` as collateral (`is-collateral-enabled` becomes false for `X`), while Alice's position still holds `X` (untracked in `position-mask`/egroup) alongside her recognized collateral/debt.
3. Market price movement or interest accrual pushes Alice's *recognized* collateral/debt below `LTV-BORROW`, making `is-healthy collateral-value debt-value current-ltvb` false — even though `X` was never part of that computation.
4. Alice calls `collateral-remove` for `X` to withdraw it (an action that cannot affect her health score since `X` isn't in the recognized assets). The call reverts with `ERR-UNHEALTHY` at line 1136, blocking withdrawal of an asset that has no bearing on her account's health. [3](#0-2)

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1107-1153)
```text
(define-public (collateral-remove (ft <ft-trait>) (amount uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (asset-id (get id asset))
        (account contract-caller)
        (collateral-receiver (match receiver recv recv contract-caller))
        (position (try! (get-position account)))
        (has-debt (> (len (get debt position)) u0)))

    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (if has-debt
        ;; HAS DEBT: Full flow with price resolution and health checks
        (let ((is-collateral-enabled (get collateral asset))
              (feeds-check (try! (write-feeds price-feeds)))
              (position-mask (get mask position))
              (pos-full (if is-collateral-enabled position (try! (get-full-position account))))
              (u-debt (accrue-user-debts (get debt pos-full)))
              (u-coll (accrue-user-collateral (get collateral pos-full)))
              (assets (get-assets position-mask))
              (curr-coll-aid (find-collateral-amount (get collateral position) asset-id))
              (removing-all (is-eq amount curr-coll-aid))
              (current-group (try! (get-egroup position-mask)))
              (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))
              (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
              (collateral-value (get collateral notional-valued-assets))
              (debt-value (get debt notional-valued-assets))
              (removed-asset-value (find-and-resolve-asset-value assets asset-id amount true)))

          (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)
          (asserts!
            (if is-collateral-enabled
                (let ((t (asserts! (>= collateral-value removed-asset-value) ERR-INSUFFICIENT-COLLATERAL))
                      (post-removal-collateral-value (- collateral-value removed-asset-value)))
                  (if removing-all
                      (let ((future-mask (bit-and position-mask (bit-not (pow u2 asset-id)))))
                        (try! (is-healthy-with-mask post-removal-collateral-value debt-value future-mask)))
                      (is-healthy post-removal-collateral-value debt-value current-ltvb)))
                (let ((oracle-data (get oracle asset))
                      (price (unwrap! (price-resolve oracle-data) ERR-DISABLED-COLLATERAL-PRICE-FAILED))
                      (decimals (get decimals asset))
                      (user-amount (find-collateral-amount (get collateral pos-full) asset-id))
                      (disabled-notional (normalize (* user-amount price) decimals false))
                      (removal-notional (normalize (* amount price) decimals true))
                      (total-collateral-value (+ collateral-value disabled-notional)))
                  (asserts! (>= total-collateral-value removal-notional) ERR-INSUFFICIENT-COLLATERAL)
                  (is-healthy (- total-collateral-value removal-notional) debt-value current-ltvb)))
```
