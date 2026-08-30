Found: `status-multi` in `mainnet/contracts/registry/v0-assets.clar` (line 163-166) is a direct analog to the `matchOrders` array-length-mismatch bug class — it uses Clarity's `map` over two parallel lists (`ids` and a fixed 64-element `mask`) without validating caller-controlled `ids` against the fixed-length `mask`, and it is reachable from `market.clar`'s unprivileged entry points.

### Title
Unvalidated variable-length caller-supplied `ids` list combined with a fixed 64-element mask in `status-multi` can silently misalign per-asset status results - ([File: mainnet/contracts/registry/v0-assets.clar])

### Summary
`status-multi` in `v0-assets.clar` zips a caller/market-supplied list of asset ids (`(list 64 uint)`, 0–64 elements) against an internally generated, always-64-length list (`uint-to-list-u64 enabled-mask`) using Clarity's `map`, which truncates to the shorter list — mirroring the `matchOrders` "parallel arrays of mismatched length silently truncate/misalign" bug class from the external report.

### Finding Description
`status-multi` is defined as: [1](#0-0) 

```
(define-read-only (status-multi (ids (list 64 uint)))
 (let ((enabled-mask (get-bitmap))
       (mask (uint-to-list-u64 enabled-mask)))
    (if (is-eq (len ids) u0) (list ) (map unwrap-status ids mask))))
```

`mask` is always constructed via `uint-to-list-u64`, which folds over the constant `ITER-UINT-64` (indices `u0`..`u63`) and therefore is always exactly 64 elements long [2](#0-1) . `ids`, however, is caller-supplied and can be any length from 1 to 64. Clarity's `map` over two lists of different lengths iterates only up to the shorter list's length (i.e., `min(len(ids), len(mask))`), analogous to how `matchOrders` in the Exchange.sol report silently drops/ignores excess elements of the longer array rather than reverting or asserting equal lengths.

Because `unwrap-status` pairs `ids[i]` with `mask[i]` (both consumed positionally) [3](#0-2) , and `mask` is not actually the caller's per-id status but a fixed vector `[0..63]` unrelated in ordering to `ids`, any caller passing an `ids` list whose length or ordering doesn't correspond 1:1 to `[0..63]` will get a `status` computation for the wrong "id" context in `unwrap-status`. In practice `unwrap-status(id, enabled-mask)` computes `collateral`/`debt` flags using `id` for bit position AND uses the second parameter (element of `mask`, which is actually just `0..63`, not `enabled-mask`) as the `enabled-mask` argument — meaning it substitutes `0,1,2,...` (loop index values) in place of the real bitmap when `mask[i] != enabled-mask`. This function is called from the market's core notional-evaluation path: `get-assets` invokes `get-status-multi` → `status-multi` with an `ids` list of collateral-relevant asset ids derived from bitmasking [4](#0-3) [5](#0-4) .

### Impact Explanation
`get-assets`/`status-multi` results (specifically the `collateral`/`debt` boolean flags) feed directly into `get-notional-evaluation`, which computes `collateral-value` and `debt-value` used for health checks in `borrow`, `collateral-remove`, and `liquidate` [6](#0-5) . If the positional misalignment between `ids` and `mask` causes an asset's `collateral`/`debt` flag to be computed incorrectly (e.g., an asset intended as non-collateral being reported collateral-eligible, or vice versa), this could corrupt health-check accounting and enable borrowing beyond the true collateral value or block/allow liquidations incorrectly — landing on protocol insolvency / theft of funds at rest (Critical), or temporary freezing of funds if it incorrectly blocks legitimate operations.

### Likelihood Explanation
The `ids` list passed into `status-multi` in the current call path (`get-assets`) is always internally derived via `mask-to-list-collateral` from the enabled bitmap, so under the existing single call-site it is not attacker-freely-chosen and lengths are naturally bounded/consistent with the mask. This significantly limits real-world exploitability through the currently-wired path; the vulnerability is latent (present in the function's contract, unchecked assumption about mask/ids correspondence) rather than trivially triggerable through today's production call graph. I could not find another production entry point that lets an ordinary principal supply an arbitrary/attacker-chosen `ids` list of a length that would trigger observable misalignment, so likelihood is Low given current wiring, though the missing invariant is a genuine analog to the reported bug class.

### Recommendation
Add an explicit assertion that `(len ids)` equals the expected fixed length (or refactor `unwrap-status`/`status` to not rely on positional pairing with an unrelated `0..63` index list) before calling `map unwrap-status ids mask`, e.g. `(asserts! (is-eq (len ids) (len mask)) ERR-INVALID-ID)`, or better, pass `enabled-mask` directly into `status` per id instead of zipping with a derived index list.

### Proof of Concept
Given `status-multi` is `read-only` and not currently reachable with attacker-controlled `ids` in production (only via `get-assets`'s internally-derived, mask-consistent list), a concrete on-chain PoC path is not confirmed in `mainnet/contracts/**`. A conceptual PoC would require identifying or adding a call path where `status-multi` is invoked directly with a caller-supplied `ids` argument of length < 64 and observing that returned `collateral`/`debt` flags correspond to the wrong bit positions, then feeding that into `get-notional-evaluation` to demonstrate skewed collateral/debt totals. Because I could not confirm such a directly-reachable, unprivileged production call path with independently-controllable `ids` beyond `get-assets`' internal usage, this finding should be treated as **Pending/uncertain likelihood** — a Devin session with contract-execution/test tooling would be needed to confirm exploitability end-to-end.

### Citations

**File:** mainnet/contracts/registry/v0-assets.clar (L80-89)
```text
(define-private (uint-to-list-u64 (val uint))
  (let ((init { val: val, result: (list) })
        (out (fold iter-uint-to-list-u64 ITER-UINT-64 init)))
    (get result out)))

(define-private (iter-uint-to-list-u64 (i uint) (acc { val: uint, result: (list 64 uint) }))
  (let ((val (get val acc))
        (result (get result acc))
        (next (as-max-len? (append result val) u64)))
    { val: val, result: (unwrap-panic next) }))
```

**File:** mainnet/contracts/registry/v0-assets.clar (L111-120)
```text
(define-private (unwrap-status (id uint) (enabled-mask uint))
  (unwrap-panic (status id enabled-mask))
)

(define-private (status (id uint) (enabled-mask uint))
  (let ((entry (try! (lookup id)))
        (debt-position (mask-pos id false))
        (is-collateral (> (bit-and enabled-mask (pow u2 id)) u0)) ;; 0 offset
        (is-debt (> (bit-and enabled-mask (pow u2 debt-position)) u0)))
    (ok (merge entry { id: id, collateral: is-collateral, debt: is-debt }))))
```

**File:** mainnet/contracts/registry/v0-assets.clar (L163-166)
```text
(define-read-only (status-multi (ids (list 64 uint)))
 (let ((enabled-mask (get-bitmap))
       (mask (uint-to-list-u64 enabled-mask)))
    (if (is-eq (len ids) u0) (list ) (map unwrap-status ids mask))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L457-459)
```text
(define-private (get-status-multi (ids (list 64 uint)))
  (contract-call? .v0-assets status-multi ids))

```

**File:** mainnet/contracts/market/v0-4-market.clar (L482-492)
```text
(define-private (get-assets (mask-user uint))
  (let ((mask-enabled (get-enabled-bitmap))
        (safe-mask (user-safe-mask mask-user mask-enabled))
        (iter (mask-to-list-collateral safe-mask))
        (assets-list (get-status-multi iter))
        (oracles-list (map get-oracle assets-list))
        ;; Extract asset-ids for price resolution
        (asset-ids (map get-asset-id assets-list))
        ;; Use internal price resolution
        (prices-list (unwrap-panic (price-multi-resolve oracles-list asset-ids))))
    (map merge-price assets-list prices-list)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1261-1296)
```text
        (current-group (try! (get-egroup mask)))
        (current-ltvb (buff-to-uint-be (get LTV-BORROW current-group)))

        ;; LTV
        (notional-valued-assets (get-notional-evaluation { position: position, assets: assets }))
        (collateral-value (get collateral notional-valued-assets))
        (debt-value (get debt notional-valued-assets)))

    ;; preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (get debt asset) ERR-BORROW-DISABLED)
    (asserts! (is-healthy collateral-value debt-value current-ltvb) ERR-UNHEALTHY)

    ;; Calculate FUTURE debt (after adding this debt)
    ;; For debt: bit position = asset-id + 64 (DEBT-OFFSET)
    (let ((future-mask (bit-or mask (pow u2 (+ asset-id DEBT-OFFSET))))
          (future-group (try! (get-egroup future-mask)))
          ;; Per-egroup borrow disable check (uses FUTURE egroup, not current)
          ;; Each bit in BORROW-DISABLED-MASK corresponds to a debt asset ID (NOT offset by 64)
          (disabled-borrow-mask (get BORROW-DISABLED-MASK future-group))
          (debt-increase (try! (get-asset-value asset amount true)))
          (debt-post-increased (+ debt-value debt-increase)))

    ;; Check if this specific asset is disabled for borrowing in the FUTURE egroup
    (asserts! (is-eq (bit-and disabled-borrow-mask (pow u2 asset-id)) u0) ERR-EGROUP-ASSET-BORROW-DISABLED)
    ;; postconditions
    (asserts! (try! (is-healthy-with-mask collateral-value debt-post-increased future-mask)) ERR-UNHEALTHY)

    (try! (vault-system-borrow asset-id amount funds-receiver))
    (let ((scaled-debt-added (convert-to-scaled-debt asset-id amount true))
          (borrow-index (get index (unwrap-panic (get-cached-indexes asset-id)))))
      (try! (contract-call? .v0-market-vault
                            debt-add-scaled
                            account
                            scaled-debt-added
                            asset-id))
```
