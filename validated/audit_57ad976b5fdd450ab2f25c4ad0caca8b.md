### Title
Borrower can front-run `liquidate` with a self-borrow to trigger the same-block anti-flashloan guard and block their own liquidation - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
The market's `liquidate` function contains an anti-flashloan/anti-frontrunning guard that reverts if the borrower's position was borrowed against in the same block as the liquidation attempt. An unprivileged borrower can weaponize this exact guard against itself: upon seeing an incoming `liquidate` transaction (with fresh oracle price updates) in the mempool, the borrower front-runs it with a trivial `borrow` call on their own position. If that borrow still passes health checks under the stale/pre-update price, it updates `last-borrow-block` to the current block height, which then causes the pending `liquidate` call to revert.

### Finding Description
`liquidate()` reads the borrower's position and enforces: [1](#0-0) 

which checks `last-borrow-block` recorded on the borrower's obligation record and reverts with `ERR-LIQUIDATION-BORROW-SAME-BLOCK` if a borrow happened in the same `stacks-block-height`. This field is set on every debt-increasing operation: [2](#0-1) 

The comment for the guard states its purpose is anti-flashloan/frontrunning protection ("This blocks flash-loan based attacks where user borrows + gets liquidated in same block"), analogous to the PartyGovernance `lastBurnTimestamp == block.timestamp` check that was meant to close a snapshot-exploit window.

Exactly as in the referenced report, the same defensive check can be turned into a griefing primitive by the party it was meant to constrain: the borrower (an ordinary, unprivileged principal calling the market's own borrow entry point) can submit a minimal borrow transaction that still passes the position's health check under the currently cached/stale price (before the liquidator's `write-feeds`/price update inside `liquidate` lands), thereby stamping `last-borrow-block = stacks-block-height` on their own obligation record. Any `liquidate` call targeting that same borrower in that same block then unconditionally reverts on the `same-block-check` assertion, regardless of how the fresh oracle price affects the account's health.

### Impact Explanation
A borrower who is about to become liquidatable (or has just become liquidatable due to a price move delivered inside the liquidator's own transaction) can repeatedly front-run every incoming `liquidate` call, block after block, with a self-borrow of negligible size, stalling liquidation of an under-collateralized position indefinitely. This directly protects the borrower's collateral/yield from seizure and, if sustained while the market continues to move against the position, risks the debt exceeding collateral value, i.e. protocol insolvency / bad debt — the same class of impact the original `accept()`/`propose()` DoS threatened for governance, but here applied to the market's core health/liquidation path.

### Likelihood Explanation
The attacker needs no privileged role, only the ability to hold a position and call the market's own borrow entry point, and to observe the liquidator's transaction in the mempool to front-run it — a standard MEV/front-running capability, not a chain-level attack. The only constraint is that the self-borrow must still pass the position's own health check under pre-update price/state, which is plausible in the narrow window right as a position crosses into liquidatable territory (the same snapshot-timing gap the guard itself was built to exploit-proof against for flashloans, now abusable in reverse).

### Recommendation
Do not use a hard same-block revert as the anti-flashloan defense on the liquidation path. Instead, gate on whether the *specific* debt/asset being liquidated was borrowed in the same block (rather than reverting the entire liquidation for the whole position), or track same-block state per-asset instead of per-account, or replace the block-based guard with a check that compares the health computed at the start of the block for that specific action against a flash-loan flag rather than a broad borrower-controlled `last-borrow-block` timestamp that any legitimate self-borrow can set.

### Proof of Concept
1. Borrower's position approaches the partial-liquidation LTV threshold.
2. Liquidator broadcasts `liquidate(borrower, ..., price-feeds: (some ...))`, which will update oracle prices and push the position over `LTV-LIQ-PARTIAL`.
3. Borrower observes this in the mempool and front-runs it with a call to the market's borrow entry point for a minimal amount, which succeeds under the currently cached (stale) price/health and sets `last-borrow-block` to the current `stacks-block-height` via `debt-add-scaled` [3](#0-2) .
4. The liquidator's `liquidate` transaction lands afterward in the same block; `same-block-check` fails because `last-borrow-block` equals `stacks-block-height`, reverting with `ERR-LIQUIDATION-BORROW-SAME-BLOCK` [4](#0-3) .
5. Borrower repeats step 3 in each subsequent block to indefinitely stall liquidation of an under-collateralized position.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1428-1435)
```text
    ;; Oracle frontrunning protection: prevent same-block liquidation
    ;; This blocks flash-loan based attacks where user borrows + gets liquidated in same block
    (last-borrow-block (get last-borrow-block position))
    (same-block-check (asserts! (not (is-eq last-borrow-block stacks-block-height)) ERR-LIQUIDATION-BORROW-SAME-BLOCK))

    ;; health check (FAIL-FAST) 
    ;; Check position is liquidatable BEFORE calling calc-liq-factor
    (health-check  (asserts! (>= current-ltv ltv-liq-partial) ERR-HEALTHY))
```

**File:** local-testing/contracts/market/market-vault.clar (L442-456)
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

    (try! (check-impl-auth))
    (asserts! (not (get debt-add states)) ERR-PAUSED)
    (asserts! (> scaled-amount u0) ERR-AMOUNT-ZERO)

    (insert updated-entry)
```
