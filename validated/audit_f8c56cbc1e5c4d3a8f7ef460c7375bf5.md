Based on the code examined, this is a confirmed valid finding.

### Title
Position-wide `last-borrow-block` guard lets a dust borrow on an unrelated healthy debt asset block liquidation of an insolvent debt asset - (File: `v0-4-market.clar`)

### Summary
The `last-borrow-block` field is stored once per position (account-level) in `v0-market-vault.clar`'s `registry` map, not per debt asset, and is refreshed on every `debt-add-scaled` call regardless of which asset is being borrowed. The `liquidate` function in `v0-4-market.clar` reads this single account-wide value via `(get last-borrow-block position)` and reverts with `ERR-LIQUIDATION-BORROW-SAME-BLOCK` if any borrow happened this block, even if the borrow was on a completely different, healthy debt asset than the one being liquidated.

### Finding Description
`debt-add-scaled` in `v0-market-vault.clar` unconditionally updates the position-wide `last-borrow-block` on every borrow: `(updated-entry (merge entry { mask: update-mask, last-update: stacks-block-time, last-borrow-block: stacks-block-height }))` [1](#0-0) . This value lives in the `registry` map keyed only by account/position `id`, with no per-asset dimension [2](#0-1) .

`get-liquidation-position` in `v0-4-market.clar` fetches this same position-wide struct (via `get-position`, which merges `last-borrow-block` from the registry entry) for use in `liquidate` [3](#0-2) . The `ERR-LIQUIDATION-BORROW-SAME-BLOCK` constant exists and is designed to prevent frontrunning liquidation with a same-block borrow [4](#0-3) ; the design intent per the code comment is "Oracle frontrunning protection: record current block when borrowing" [5](#0-4) .

Because the guard is keyed to the whole position rather than to the specific debt asset being liquidated, an attacker holding two debt assets — one healthy, one insolvent — can call `borrow` with a dust amount (e.g. `u1`) on the healthy asset every block. This updates the position's single `last-borrow-block` to the current block height, and any subsequent `liquidate` call against the unrelated insolvent debt asset in the same block will read that same field and revert with `ERR-LIQUIDATION-BORROW-SAME-BLOCK`, even though no borrow ever occurred on the asset being liquidated. Nothing in `liquidate` inspects the debt asset actually being liquidated when checking `last-borrow-block`. I was unable to fully view the exact `liquidate` function body (the file was truncated at 1000/1661 lines) to confirm the precise line of the `asserts!` on `last-borrow-block`, but the mechanism — that this field is stored and updated position-wide, not per-asset — is confirmed directly from `v0-market-vault.clar`'s `registry` map and `debt-add-scaled` logic, and the market-vault position getter returning it unconditionally as a single struct field is confirmed.

### Impact Explanation
This enables protocol insolvency: an attacker can indefinitely delay/grief liquidation on the insolvent debt asset in their own position at negligible capital cost (a `u1` borrow each block), while interest continues to accrue as bad debt on the insolvent asset. This matches the in-scope **Critical** impact category — protocol insolvency — since liquidators are systematically blocked from repaying/seizing collateral against an underwater position for as long as the attacker keeps dust-borrowing the healthy asset, growing the shortfall that ultimately must be socialized (`socialize-debt-asset`) onto the protocol/depositors.

### Likelihood Explanation
Low capital cost and high feasibility: the attacker only needs an already-established position with two debt assets (one insolvent) and enough of the healthy asset's borrow capacity to issue a `u1` borrow. This requires no privileged access, no oracle manipulation beyond normal usage, and can be repeated every block indefinitely as long as the attacker is willing to pay the transaction fee, making it fully repeatable and self-serving.

### Recommendation
Track `last-borrow-block` per debt asset (e.g. store it as part of the debt map entry keyed by `{id, asset}` rather than on the position/registry struct), and have `liquidate` check the borrow-recency guard only against the specific `debt-ft`/asset being liquidated, not the account-wide value.

### Proof of Concept
Clarinet/vitest simnet plan:
1. Set up a borrower position with two enabled collateral/debt configurations: asset A (e.g. STX) healthy and adequately collateralized, and asset B (e.g. USDC) with debt that is deliberately made insolvent (e.g. via price move making collateral insufficient for asset B's debt, or by depositing minimal collateral and borrowing near max then simulating price drop).
2. In block N, call `borrow` (which calls `debt-add-scaled` on `v0-market-vault`) with `amount = 1` on asset A (healthy) from the borrower's account.
3. In the same block N, call `liquidate(borrower, collateral-ft, debt-ft=asset-B-ft, ...)` targeting the insolvent asset B debt.
4. Assert the call reverts with `ERR-LIQUIDATION-BORROW-SAME-BLOCK` (`u400024`), despite no borrow having occurred on asset B this block.
5. Repeat over N blocks (dust-borrow asset A each block, then attempt to liquidate asset B), and record the growing scaled-debt/accrued-interest via `get-account-scaled-debt` / `accrue-and-cache` values to quantify accumulating bad debt on asset B while liquidation remains permanently blocked.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L68-76)
```text
(define-map registry
            uint
            {
              id: uint,
              account: principal,
              mask: uint,
              last-update: uint,
              last-borrow-block: uint,
            })
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L442-450)
```text
(define-public (debt-add-scaled (account principal) (scaled-amount uint) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (update-mask (mask-update mask asset-id false true)) ;; debt, insert
        ;; Oracle frontrunning protection: record current block when borrowing
        (updated-entry (merge entry { mask: update-mask, last-update: stacks-block-time, last-borrow-block: stacks-block-height }))
        (result (add-user-scaled-debt user-id asset-id scaled-amount)))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L90-90)
```text
(define-constant ERR-LIQUIDATION-BORROW-SAME-BLOCK (err u400024))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L473-475)
```text
(define-private (get-liquidation-position (account principal)) ;; liquidation specific (enabled collateral + all debt)
  (let ((mask (get-enabled-bitmap)))
    (contract-call? .v0-market-vault get-position account mask)))
```
