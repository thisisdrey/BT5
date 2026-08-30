### Title
Withdrawal of disabled collateral is permanently blocked when its oracle fails, freezing unrelated funds - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`collateral-remove` in `v0-4-market.clar` forces any user who (a) holds a disabled-collateral asset and (b) has *any* open debt anywhere in their position, to obtain a successful oracle price for that disabled asset before they are allowed to withdraw *any* amount of it — even though the disabled asset does not contribute to their borrowing capacity at all. If the oracle for that disabled asset is unavailable (removed, stale, or otherwise failing), the withdrawal reverts unconditionally with `ERR-DISABLED-COLLATERAL-PRICE-FAILED`, exactly mirroring the Wise-Lending `checksWithdraw` pattern where withdrawal of an uncollateralized/blacklisted pool token is blocked purely because the user has an unrelated open borrow position.

### Finding Description
In `collateral-remove`: [1](#0-0) 

when the user has debt (`has-debt`), the function branches on `is-collateral-enabled` (whether the asset is currently registered as active collateral): [2](#0-1) 

If the asset is **disabled** as collateral, the code takes the disabled branch, which unconditionally resolves the oracle price for that disabled asset via `unwrap!`, reverting with `ERR-DISABLED-COLLATERAL-PRICE-FAILED` if the resolution fails: [3](#0-2) 

Because disabled assets are, by definition, excluded from the enabled-collateral bitmap used for normal LTV/health calculations (`get-position` only counts enabled collateral, `get-full-position` pulls in the disabled amount only for this extra check), the disabled asset's price is not required for the user's actual solvency — it is used purely as an additional conservative check ("the sum of enabled collateral plus latent disabled collateral value must still cover the amount removed"). Requiring a live oracle for an asset that has already been taken out of the active collateral set is fragile: if that oracle feed becomes stale or is deregistered (a normal consequence of decommissioning an asset — assets that are disabled as collateral are frequently also the ones whose price feeds stop being maintained), any user who holds that disabled token as collateral and has *any* other unrelated debt open is completely blocked from withdrawing it, regardless of the withdrawal amount or of how well-collateralized their actual debt is.

This is structurally identical to the Wise-Lending bug: `checksWithdraw` blocked withdrawal of an uncollateralized/blacklisted pool token whenever the caller had *any* open borrow position, even though that specific token was not backing the borrow. Here, the disabled-collateral price check plays the same role as the "blacklisted" check in Wise-Lending, and `has-debt` (computed from the *entire* position, not the specific asset being withdrawn) plays the role of `OpenBorrowPosition`.

### Impact Explanation
This falls under "temporary freezing of funds": a user's disabled-collateral holdings become inaccessible for as long as (a) they have any open debt and (b) the disabled asset's oracle is unavailable. The user's only mitigation is to fully repay all debt (which flips `has-debt` to `false` and takes the no-price-check branch), which may not be feasible for the user (insufficient liquidity to repay, or the debt asset itself is frozen/paused). Until the DAO restores the oracle feed or the user manages to fully clear all debt, the disabled collateral is frozen — an unprivileged principal (an ordinary borrower who is simply asked to hold a disabled collateral type) triggers this purely through normal market usage, with no DAO compromise involved.

### Likelihood Explanation
Likelihood is moderate: it requires (1) an asset to be disabled as collateral by the DAO (an intended, supported lifecycle event) while (2) its oracle feed subsequently becomes unavailable/stale (also a plausible and even expected consequence of decommissioning an asset), and (3) a holder of that asset with any unrelated open debt attempting to withdraw. Given that assets are commonly disabled precisely because they are being phased out (including their price feeds), this combination is likely to occur during any real decommissioning event, not just as a contrived edge case.

### Recommendation
Do not require a live oracle price for the disabled asset when the amount of debt to which it is not contributing collateral value is already covered by the enabled-collateral value. Specifically:
- Skip the disabled-asset price resolution entirely when the enabled-collateral value alone already keeps the position healthy after the withdrawal (i.e., only fall back to resolving the disabled asset's price if strictly necessary to prove sufficiency), or
- Allow disabled-collateral withdrawal to succeed using a cached/last-known price, or gracefully treat an oracle failure as "disabled asset contributes zero value" (fail-safe to the conservative side) instead of reverting the whole withdrawal.

### Proof of Concept
1. DAO disables `sBTC` as collateral (asset marked `collateral: false` in `assets` registry) as part of a decommissioning process, and the sBTC price feed subsequently becomes stale/unregistered.
2. Alice holds sBTC as a leftover disabled-collateral position and has an unrelated, fully healthy USDC debt backed entirely by other enabled collateral (e.g., STX).
3. Alice calls `collateral-remove` on the sBTC ft-trait to withdraw her disabled sBTC.
4. Because `has-debt` is `true` (she has USDC debt, unrelated to sBTC), the function takes the `is-collateral-enabled = false` branch and calls `unwrap!` on `price-resolve` for the sBTC oracle: [4](#0-3) 
5. The oracle resolution fails (stale/removed feed) and the entire transaction reverts with `ERR-DISABLED-COLLATERAL-PRICE-FAILED`, regardless of amount, even though her sBTC holding plays no role in her USDC debt's health.
6. Alice's sBTC is frozen until either the oracle is restored or she fully repays her unrelated USDC debt.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1107-1120)
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1136-1153)
```text
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
