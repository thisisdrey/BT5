### Title
Interest-accrual bypass via stale `index-cache` reuse when `stacks-block-time` fails to strictly increase between blocks - (File: `mainnet/contracts/market/v0-4-market.clar`)

### Summary
`market.clar`'s per-block liquidity-index cache assumes `stacks-block-time` changes on every new block to guarantee freshness. `accrue-and-cache` treats a cache hit on `{timestamp, aid}` as authoritative and skips the real `vault-accrue` call entirely [1](#0-0) . The docs explicitly state the invalidation model relies on "New block → new timestamp → cache miss → fresh accrual" and claims this "eliminates stale data risks" [2](#0-1) . This is precisely the class of bug in the Nano report: code assumes a monitored/invalidating condition (there, election existence guarded only by a non-atomic check; here, cache freshness guarded only by the timestamp key) always holds, without any fallback verification, so if the assumption is violated the stale/incorrect state is silently used instead of triggering a fresh, correct computation.

### Finding Description
`index-cache` is a **global** map, not scoped per caller, keyed only by `{ timestamp: stacks-block-time, aid: uint }` [3](#0-2) . `accrue-and-cache` is the single choke point through which all borrow/repay/collateral-add/collateral-remove/liquidation flows obtain vault liquidity/borrow indexes; on a cache hit it returns the stored value directly and never calls `vault-accrue` [1](#0-0) . Callers such as `borrow` rely on this cache being correctly primed before resolving ztoken prices, as the code comments themselves acknowledge was previously an ordering hazard ("Step 4: NOW safe to resolve prices (cache is populated)") [4](#0-3) .

The correctness of this scheme depends entirely on the invariant that `stacks-block-time` strictly increases across sequential blocks so that a cache entry from block N can never be read as if it were fresh in block N+1. Nothing in `accrue-and-cache` cross-checks this against the vault's own last-accrual timestamp or block height; it trusts the map key alone. If two sequential blocks are ever produced with the same `stacks-block-time` value (e.g., due to sub-second/fast block production or any clock behavior that does not guarantee a strictly monotonic increase every block), any transaction in the second block that touches an asset whose index was already cached in the first block will silently reuse the first block's index and skip the real `vault-accrue` call, exactly as the Nano bug allowed stale in-memory state (the "tally"/"election") to be read as if it were still valid because the invalidating check was not performed atomically with its use.

### Impact Explanation
Because `accrue-and-cache` gates all liquidity/borrow index reads used for health checks, ztoken price resolution (`resolve-ztoken`), and scaled-debt conversion, a skipped accrual means collateral/debt valuations and debt balances used for borrow/repay/liquidation decisions in the affected block are computed against an index that has not incorporated that block's true elapsed-time interest. This can let a borrower under-repay debt relative to what should be owed, or overvalue ztoken collateral relative to its true worth, both of which are forms of value leaking out of protocol accounting - i.e., theft/misappropriation of unclaimed yield, satisfying the in-scope "High" impact bar (theft of unclaimed yield). Repeated or compounding occurrences across the reserve-factor treasury-minting path (`vaults.md` describes treasury shares minted on every real accrue) would also permanently under-mint the DAO treasury's reserve share for any accrual event that is skipped, a permanent loss of protocol-owned yield.

### Likelihood Explanation
This does not require any privileged action or DAO compromise - it is triggerable by an ordinary principal simply timing their own transaction in the block(s) where the timestamp anomaly occurs, or by any concurrent normal user activity. The only externally uncertain variable is how often/whether `stacks-block-time` can repeat across consecutive blocks on Stacks; I could not verify this guarantee from the indexed documentation, so likelihood is rated dependent on that platform-level property rather than proven with certainty from this repo alone.

### Recommendation
Do not treat a cache hit on `{timestamp, aid}` as sufficient proof of freshness. Cross-validate against the vault's own last-accrual timestamp/index before trusting the cache, or fall back to invoking `vault-accrue` whenever the vault's persisted last-update timestamp is older than the current transaction's actual elapsed time, regardless of whether an `index-cache` entry already exists for the current `stacks-block-time` key.

### Proof of Concept
Not independently reproducible from the indexed contents alone: exploitation depends on whether two sequential Stacks blocks can share an identical `stacks-block-time`, which is a platform/consensus property not verifiable from this codebase's index. Given the size limits on the index, some vault-contract (`vault-*.clar`) accrual-timestamp-comparison logic may not be fully visible here; a Devin session with full repository access would be needed to confirm whether `vault-accrue` independently re-validates elapsed time and to construct a concrete two-block PoC.

### Citations

**File:** mainnet/contracts/market/v0-4-market.clar (L112-115)
```text
;; -- Index cache (for accrual)
(define-map index-cache
  { timestamp: uint, aid: uint }
  { index: uint, lindex: uint })
```

**File:** mainnet/contracts/market/v0-4-market.clar (L245-257)
```text
(define-private (accrue-and-cache (aid uint))
  (let ((cache-key { timestamp: stacks-block-time, aid: aid })
        (cached? (map-get? index-cache cache-key)))

    (match cached?
      ;; cache HIT: return cached value (1 read only)
      cached-indexes (ok cached-indexes)

      ;; cache MISS: accrue and cache (vault-accrue now returns indexes)
      (let ((indexes (try! (vault-accrue aid))))
        ;; store in cache
        (map-set index-cache cache-key indexes)
        (ok indexes)))))
```

**File:** mainnet/contracts/market/v0-4-market.clar (L1246-1257)
```text
        ;; Step 1: Get position WITHOUT resolving prices
        (position (try! (get-position account)))
        (mask (get mask position))
        
        ;; Step 2: Accrue user's positions (populates cache for ztokens)
        (u-debt (accrue-user-debts (get debt position)))
        (u-coll (accrue-user-collateral (get collateral position)))
        
        ;; Step 3: Accrue the asset being borrowed (needed for index access)
        (unused (accrue-and-cache asset-id))
        
        ;; Step 4: NOW safe to resolve prices (cache is populated)
```

**File:** docs/market.md (L627-633)
```markdown
### Cache Invalidation

Cache is **automatically invalidated** each block:
- Cache key includes `stacks-block-time` (block timestamp)
- New block → new timestamp → cache miss → fresh accrual
- No manual invalidation needed
- Eliminates stale data risks
```
