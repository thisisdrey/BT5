Confirmed: `add-user-collateral` performs `map-set collateral key updated-collateral-amount` immediately when it's called [1](#0-0) , and this call happens inside the `let` bindings of `collateral-add`, i.e. before the function body's `asserts!` checks and before the external callback via `receive-tokens` [2](#0-1) . The obligation's `mask` (which records that this asset is active as collateral, used for egroup resolution and health checks) is only persisted afterward, via `(insert updated-entry)`, which runs *after* the external `ft` contract call [3](#0-2) .

### Title
Position mask update occurs after the external token-transfer callback in `collateral-add`, splitting collateral-accounting state across a reentrancy window - (File: `mainnet/contracts/market/v0-market-vault.clar`, also `local-testing/contracts/market/market-vault.clar`)

### Summary
`collateral-add` in `market-vault`/`v0-market-vault` mirrors the exact bug class flagged in the Timeswap `mint()` finding: an external call (`receive-tokens`, which invokes an arbitrary caller-supplied `<ft-trait>` contract) sits in the middle of the function, between two related but separately-committed state changes — the collateral amount map (committed before the call) and the account's position bitmask (committed after the call).

### Finding Description
`collateral-add` is structured as:
1. `let`-bindings compute `updated-mask`/`updated-entry` and call `add-user-collateral`, which **immediately** does `map-set collateral key updated-collateral-amount` [1](#0-0)  — this runs before any of the function's `asserts!` and before the external call.
2. The function body then calls `(try! (receive-tokens ft amount account))`, which does `(contract-call? asset transfer amount account current-contract none)` [4](#0-3)  — an external call into a contract supplied by the caller as an `<ft-trait>` parameter.
3. Only after that external call returns does the function persist the mask/`last-update` via `(insert updated-entry)` [3](#0-2) .

This is guarded by `check-impl-auth`, restricting direct calls to the registered `impl` (the `market.clar`/`v0-4-market.clar` contract) [5](#0-4) . The outer `market.clar` wrapper does validate that `ft` matches the registry's asset address via `get-asset ft-address` before forwarding into `market-vault collateral-add` [6](#0-5) , so the transferred token contract itself is a registered asset, not an arbitrary attacker contract. That constrains, but does not eliminate, the CEI ordering problem: within `market-vault`, the `collateral` map has already been incremented for `user-id`/`asset-id` (step 1) at the moment `receive-tokens` (a `contract-call?` into the registered token, e.g. an `ft-trait` SIP-010 implementation) executes, while the mask flip that marks this asset as "active collateral" for the account is deferred until after that call returns. Any reentrancy into `market-vault` (or into `market.clar`, which is the sole authorized caller) triggered from within the token's `transfer` execution would observe a collateral balance that has already been bumped but a stale mask (i.e., a state where `get-collateral` reports the new amount but `get-position`/mask-derived egroup and health calculations, which read the mask to determine which assets are queried, have not yet flipped this asset's collateral bit) — an inconsistent intermediate state exposed to any code that executes as part of the token transfer.

### Impact Explanation
If reachable, this state split could let a reentrant read (e.g., a nested `borrow`/`collateral-remove`/`liquidate` call, or any code executing during the token's `transfer`) observe collateral credited to an internal accounting map without the corresponding mask bit that egroup resolution and health checks depend on, or vice versa for `collateral-remove`/`debt-add-scaled`, where the mask is committed but the amount/debt map is finalized after further external interaction. This falls into the vault share math / collateral accounting / egroup resolution category the rules keep in scope, and if an inconsistency is actually exploitable it could enable manipulation of health checks leading to under-collateralized borrowing (theft of funds) or a frozen/incorrect position (temporary freezing of funds).

### Likelihood Explanation
Likelihood is low-to-moderate and unconfirmed. The registered assets (`stx`, `sbtc`, `ststx`, `usdc`, `usdh`, `ststxbtc`, and their zTokens) are simple SIP-010/vault implementations reviewed in this repo (e.g., `ststx.clar`, `usdh.clar`, `wstx.clar`) whose `transfer` functions call only native `ft-transfer?`/`stx-transfer?` with no external re-entry hooks visible in the code that was inspected [7](#0-6) . I was not able to fully verify whether any registered asset's `transfer` (or a future DAO-registered asset) could contain a callback/hook capable of reentering `market.clar`/`market-vault.clar` before `insert` commits the mask — that would require enumerating every asset contract permitted by the DAO registry and confirming Clarity's `contract-call?` semantics allow reentrant calls to the same or related contracts within one transaction, which is outside what could be conclusively confirmed with the available index.

### Recommendation
Reorder `collateral-add` (and symmetrically review `collateral-remove`, `debt-add-scaled`) so that all state mutations to the `collateral`/`debt` maps and the obligation `mask`/`insert` happen together, atomically, either entirely before or entirely after the external token transfer call — mirroring the Checks-Effects-Interactions pattern recommended in the Timeswap fix. Concretely, defer `add-user-collateral`'s `map-set` until after `receive-tokens` succeeds, or move `receive-tokens` before any of the `let`-binding side effects, so a single call boundary is crossed only once, with no observable intermediate state.

### Proof of Concept
Not independently reproducible from the available code: exploitation depends on a registered SIP-010 asset's `transfer` implementation containing a reentrant hook that calls back into `market-vault`/`market` before returning, which was not found in any of the token implementations reviewed (`ststx.clar`, `usdh.clar`, `wstx.clar`). The structural CEI violation itself is directly verifiable at [8](#0-7) , but a concrete exploit transaction sequence could not be constructed without further access to confirm whether any in-scope, DAO-registered asset contract can execute attacker logic during its `transfer` call.

### Citations

**File:** mainnet/contracts/market/v0-market-vault.clar (L132-133)
```text
(define-private (check-impl-auth)
  (ok (asserts! (is-eq contract-caller (var-get impl)) ERR-AUTH)))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L198-203)
```text
(define-private (add-user-collateral (user-id uint) (asset-id uint) (amount uint))
  (let ((key { id: user-id, asset: asset-id })
        (collateral-amount (default-to u0 (map-get? collateral key))) ;; graceful default
        (updated-collateral-amount (+ collateral-amount amount)))
      (map-set collateral key updated-collateral-amount)
      updated-collateral-amount))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L256-257)
```text
(define-private (receive-tokens (asset <ft-trait>) (amount uint) (account principal))
  (contract-call? asset transfer amount account current-contract none))
```

**File:** mainnet/contracts/market/v0-market-vault.clar (L374-390)
```text
(define-public (collateral-add (account principal) (amount uint) (ft <ft-trait>) (asset-id uint))
  (let ((states (var-get pause-states))
        (entry (resolve-or-create account))
        (user-id (get id entry))
        (mask (get mask entry))
        (updated-mask (mask-update mask asset-id true true)) ;; collateral, insert
        (updated-entry (merge entry (refresh updated-mask)))
        (result (add-user-collateral user-id asset-id amount)))

    (try! (check-impl-auth))
    (asserts! (not (get collateral-add states)) ERR-PAUSED)
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)

    (try! (receive-tokens ft amount account))
    
    (insert updated-entry)

```

**File:** local-testing/contracts/market/market.clar (L1113-1113)
```text
    (let ((result (try! (contract-call? .market-vault collateral-add account amount ft asset-id))))
```

**File:** local-testing/contracts/utility/token/ststx.clar (L15-18)
```text
(define-public (transfer (amount uint) (sender principal) (recipient principal) (memo (optional (buff 34))))
  (begin
    (asserts! (is-eq tx-sender sender) err-not-token-owner)
    (ft-transfer? ststx amount sender recipient)))
```
