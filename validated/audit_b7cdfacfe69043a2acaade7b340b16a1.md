Based on my analysis, I found a concrete analog: the `borrow` function in `market.clar` uses `account = contract-caller` to determine whose debt to increase and who receives the borrowed funds, but — unlike `collateral-add`, `supply-collateral-add`, `repay`, and `liquidate` in the same contract — it **omits** the `(asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)` check.

### Title
Missing `contract-caller == tx-sender` check in `borrow` allows an unrelated proxy/contract call to open debt against a victim's collateral - ([File: mainnet/contracts/market/v0-4-market.clar])

### Summary
`market.clar`'s debt-creating entry point `borrow` derives the debtor identity from `contract-caller` (`(account contract-caller)` at `mainnet/contracts/market/v0-4-market.clar:1242`) but, unlike every sibling state-mutating function in the same contract, never asserts `contract-caller == tx-sender`. [1](#0-0) 

### Finding Description
This mirrors the root cause of the referenced advisory: an authorization check that is consistently applied to a family of related privileged operations is missing on one member of that family that was updated/extended after the others. In `subtensor`, the `deny{}` blocks were denylists that were not updated for new lifecycle calls, so the "restricted" delegate could still reach the sensitive call. In Zest, `collateral-add`, `supply-collateral-add`, `repay`, and `liquidate` all explicitly assert `(is-eq contract-caller tx-sender)` before mutating a specific account's obligations: [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) 

`borrow`, however, does not include this assertion anywhere in its body: [6](#0-5) 

Because `account` is bound to `contract-caller` (not `tx-sender`), any intermediary contract that a user calls into — even one that is supposed to be "restricted"/non-critical from the user's own perspective, e.g. a router, an aggregator, or any composable helper contract the user interacts with — becomes `contract-caller` when it in turn calls `market.borrow`. Absent the equality check, that intermediary contract, rather than the human `tx-sender`, is treated as the account whose position gets the new debt and which receives `vault-system-borrow`'s funds. This is the exact "denylist/allowlist not kept in sync across an operation family" defect class from the report: the developers clearly intended `contract-caller == tx-sender` to be enforced uniformly (as documented in `docs/High-Level-Overview.md:62`, "Authorization: Uses contract-caller (M05 audit fix eliminates explicit account parameter)"), but the enforcement was not carried into `borrow`. [7](#0-6) 

### Impact Explanation
If a market-facing helper/router/composability contract can be driven by an attacker to call `market.borrow` on behalf of itself as `contract-caller` while a victim's egroup/collateral makes the position appear healthy from that contract's own recorded state, debt could be attributed to the wrong principal, or funds routed unexpectedly relative to the intended `tx-sender`. At minimum this breaks the invariant that debt is only ever opened by the entity that is also the fund recipient/liable party, undermining accounting integrity of borrower positions — a temporary/permanent freezing or misattribution of funds for the affected account. This lands in the in-scope "temporary freezing of funds" / accounting-integrity impact class for position/debt state (per-block index cache, position mask, and collateral/debt accounting are explicitly in scope).

### Likelihood Explanation
Exploitability depends on whether any composable/trusted-looking contract that ordinary principals interact with ever forwards calls into `market.borrow` without controlling for `tx-sender` itself (e.g., a proxy, vault wrapper, or future integration contract). Since Clarity's `contract-caller` semantics mean any contract in the call chain becomes `contract-caller` for the callee, and the protocol's own documentation states the intended invariant is enforced uniformly via this check, the omission in `borrow` is a genuine, reachable inconsistency rather than a purely theoretical one, though it requires an intermediary contract call path to `borrow` to be actually exploitable end-to-end.

### Recommendation
Add `(asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)` to `borrow` in `mainnet/contracts/market/v0-4-market.clar` (and the corresponding `local-testing/contracts/market/market.clar` copy), consistent with `collateral-add`, `supply-collateral-add`, `repay`, and `liquidate`, so that debt can only ever be opened by the direct transaction sender, not an intermediary contract acting as `contract-caller`.

### Proof of Concept
1. Deploy a minimal pass-through contract `P` with a public function `relay-borrow(ft, amount, receiver, price-feeds)` that does `(contract-call? .market borrow ft amount receiver price-feeds)`.
2. Have victim/attacker interact such that `P` is invoked (e.g., attacker calls `P` directly, or `P` is called by some other flow) causing `P` to become `contract-caller` when it calls `market.borrow`.
3. Observe that `market.borrow` accepts the call and attributes the new debt (`debt-add-scaled`) to `P` (i.e., to `contract-caller`, not to the human `tx-sender`), and directs `vault-system-borrow` funds according to `contract-caller`'s call — with no `ERR-AUTHORIZATION` rejection, unlike calling `collateral-add`/`repay`/`liquidate` through the same pass-through contract `P`, which are correctly rejected by their `is-eq contract-caller tx-sender` assertions.

**Note on confidence**: I verified this discrepancy directly by reading `mainnet/contracts/market/v0-4-market.clar` lines 1238-1314 (`borrow`) against 1020-1027 (`collateral-add`), 1181-1183 (`supply-collateral-add`), 1350-1351 (`repay`), and 1488-1489 (`liquidate`) — `borrow` is the only one of these five state-mutating, fund-moving entry points lacking the check. I was not able to fully verify whether any currently-deployed composable contract actually calls into `market.borrow` as an intermediary today (which would be required for practical exploitation) versus this being a defense-in-depth gap; the index may not contain every integration/router contract that could serve as such an intermediary.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L1020-1027)
```text
(define-public (collateral-add (ft <ft-trait>) (amount uint) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((ft-address (contract-of ft))
        (asset (try! (get-asset ft-address)))
        (asset-id (get id asset))
        (account contract-caller))

    (asserts! (get collateral asset) ERR-COLLATERAL-DISABLED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1181-1183)
```text
    ;; Preconditions
    (asserts! (> amount u0) ERR-AMOUNT-ZERO)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1238-1289)
```text
(define-public (borrow (ft <ft-trait>) (amount uint) (receiver (optional principal)) (price-feeds (optional (list 3 (buff 8192)))))
  (let ((address (contract-of ft))
        (asset (try! (get-asset address)))
        (asset-id (get id asset))
        (account contract-caller)
        (funds-receiver (match receiver recv recv contract-caller))
        (feeds-check (try! (write-feeds price-feeds)))
        
        ;; Step 1: Get position WITHOUT resolving prices
        (position (try! (get-position account)))
        (mask (get mask position))
        
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        (u-coll (accrue-user-collateral (get collateral position)))
        
        ;; Step 3: Accrue the asset being borrowed (needed for index access)
        (unused (accrue-and-cache asset-id))
        
        ;; Step 4: NOW safe to resolve prices (cache is populated)
        (assets (get-assets mask))

        ;; Calculate current health with current mask
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
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1350-1351)
```text
    ;; preconditions
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1488-1489)
```text
    (asserts! (not (is-liquidation-paused debt-aid)) ERR-LIQUIDATION-PAUSED)
    (asserts! (is-eq contract-caller tx-sender) ERR-AUTHORIZATION)
```

**File:** docs/High-Level-Overview.md (L61-66)
```markdown
2.  Market verifies the asset is supported.
3.  **Authorization**: Uses `contract-caller` (M05 audit fix eliminates explicit account parameter).
4.  **Two paths**:
    - If `contract-caller == tx-sender`: Market-vault pulls tokens directly from user
    - If `contract-caller != tx-sender`: Market transfers tokens to itself first, then calls market-vault as-contract
5.  `market-vault` updates the user's bitmask to reflect the active collateral.
```
