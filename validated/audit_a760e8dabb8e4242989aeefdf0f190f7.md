### Title
Zero-balance cleanup in `Amounts<HashMap<TokenId,u128>>` causes stale re-read of `self.view.balance_of` mid-batch, inflating cached balances - (File: contracts/defuse/core/src/engine/state/cached.rs)

### Summary
`CachedState::internal_add_balance` and `internal_sub_balance` lazily seed `account.token_amounts[token_id]` from `self.view.balance_of(...)` only "if the entry is missing" (`account.token_amounts.get(&token_id).is_none()`). Because `Amounts<T>` is backed by a `DefaultMap`/"cleanup" map (`contracts/defuse/core/src/amounts.rs`, `checked_apply` via `entry_or_default`), any operation that nets a token's cached balance back to exactly `0` causes the entry to be *removed* from the map, not merely set to `0`. The next touch of that `token_id` in the same batch then re-triggers the lazy-init path and re-reads the stale, pre-batch value from `self.view`, double counting the original balance.

### Finding Description
Binding that must hold: for a given `CachedState` instance and `token_id`, after N sequential add/sub operations,
`account.token_amounts.amount_for(&token_id) == original_view_balance + Σ(deltas applied so far)`.

The lazy-init guard in both functions is: [1](#0-0) [2](#0-1) 

This assumes the map entry, once created, persists for the lifetime of the `CachedState`. But `Amounts::checked_apply` uses `entry_or_default`, backed by the "cleanup" `DefaultMap` implementation: [3](#0-2) 

The `invariant()` unit test in the same file proves this cleanup semantics directly: applying `+1` then `-1` to the same key leaves the map `is_empty()`, i.e. the key is deleted once its value returns to `0`, for both `BTreeMap`- and (via the same trait bound) `HashMap`-backed `Amounts`: [4](#0-3) 

Exploit flow (attacker holds a real, legitimate balance of 100 units of token `X` in the Verifier):
1. Submit one `MultiPayload` batch containing two intents signed by the attacker, both touching `X`, processed against a single `CachedState` instance.
2. Intent 1 fully drains the cached balance of `X` to `0` via `internal_sub_balance` (e.g. a transfer/withdraw of the attacker's entire `X` balance). This sets `account.token_amounts[X]` to `0`; the cleanup `DefaultMap` then removes the `X` entry from the map entirely.
3. Intent 2 (any operation crediting `X` back, e.g. a `token_diff`/mint/transfer-in of a small amount) calls `internal_add_balance` for `X`. Since `account.token_amounts.get(&X)` is now `None` again, the lazy-init path re-fires: `self.view.balance_of(owner_id, &X)` still returns the *original, un-mutated* on-chain value (100), because `self.view` is the immutable underlying state snapshot and is never updated by intra-batch cached mutations.
4. The cached balance becomes `100 (stale baseline) + small_credit`, instead of the correct `0 + small_credit`. The attacker's ledger balance is inflated by the full amount that was already spent/withdrawn in step 2.

No existing guard prevents this: `MultiPayload::verify`, nonce checks, and `Lock::get_mut` only govern signature/nonce/lock validity, not the internal balance-caching invariant; there is no `checked_*` arithmetic issue (the overflow checks are fine) — the bug is purely in the stale re-seed from `self.view`.

### Impact Explanation
This creates tokens out of thin air in the Verifier's internal ledger without any corresponding deposit: the attacker's cached/committed balance ends up higher than `original_balance + net_deltas`, meaning the batch's balance changes do not net to zero and the Verifier owes more than it custodies. The attacker can subsequently withdraw the inflated balance, extracting real assets (NEP-141/171/245 tokens) that were never deposited. This is repeatable per token per batch by any unprivileged signer who has a nonzero balance of any token and can construct two same-signer intents in one batch that first zero out and then re-touch that token — a Critical finding matching "a batch whose balance changes do not net to zero so the Verifier owes more than it custodies."

### Likelihood Explanation
Preconditions are trivial and fully attacker-controlled: hold any nonzero balance of a token in the Verifier, and construct a single `MultiPayload`/batch with two self-authorized intents on the same `token_id` — first one that drains the cached balance to exactly `0`, second one that credits any amount back to the same token. No privileged roles, relayer keys, or victim keys are needed; cost is one transaction with two normally-permitted intents signed by the attacker themselves.

### Recommendation
Do not rely on map-entry presence (`is_none()`) to decide whether the view baseline has already been seeded. Track "seeded" state explicitly (e.g. a separate `HashSet<TokenId>` of touched tokens, or a map value type that is never cleaned up mid-batch, e.g. wrap `token_amounts` in a plain non-cleanup map for the lifetime of `CachedState`, only applying cleanup when flushing to persistent storage at commit time).

### Proof of Concept
```rust
// contracts/defuse/core/src/engine/state/cached.rs (unit test)
// Mock StateView whose balance_of always returns a fixed non-zero baseline (e.g. 100),
// simulating the attacker's real, un-mutated on-chain balance.
#[test]
fn stale_reseed_after_zero_cleanup() {
    let mut state = CachedState::new(mock_view_with_balance(100));
    let owner = "attacker.near".parse().unwrap();
    let token = /* some TokenId X */;

    // Intent 1: drain cached balance of `token` to exactly 0.
    state.internal_sub_balance(&owner, [(token.clone(), 100)]).unwrap();
    assert_eq!(state.balance_of(&owner, &token), 0);

    // Intent 2: credit back a small amount to the SAME token in the SAME CachedState.
    state.internal_add_balance(owner.clone(), [(token.clone(), 1)]).unwrap();

    // BROKEN BINDING CHECK:
    // Correct expected value: original_balance(100) - 100(sub) + 1(add) = 1
    // Actual observed value if lazy-init re-fires from stale view: 100 (stale) + 1 = 101
    assert_eq!(
        state.balance_of(&owner, &token),
        1,
        "cached balance was re-seeded from stale view.balance_of after cleanup removed the zero entry"
    );
}
```
Running this against the current implementation is expected to fail the assertion, observing `101` instead of `1`, confirming the balance-inflation vulnerability.

### Citations

**File:** contracts/defuse/core/src/engine/state/cached.rs (L211-221)
```rust
        for (token_id, amount) in token_amounts {
            if account.token_amounts.get(&token_id).is_none() {
                account
                    .token_amounts
                    .add(token_id.clone(), self.view.balance_of(&owner_id, &token_id))
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
            account
                .token_amounts
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L243-252)
```rust
            if account.token_amounts.get(&token_id).is_none() {
                account
                    .token_amounts
                    .add(token_id.clone(), self.view.balance_of(owner_id, &token_id))
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
            account
                .token_amounts
                .sub(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
```

**File:** contracts/defuse/core/src/amounts.rs (L136-141)
```rust
    #[inline]
    fn checked_apply(&mut self, k: T::K, f: impl FnOnce(T::V) -> Option<T::V>) -> Option<T::V> {
        let mut a = self.0.entry_or_default(k);
        *a = f(*a)?;
        Some(*a)
    }
```

**File:** contracts/defuse/core/src/amounts.rs (L289-294)
```rust
        assert!(
            Amounts::<BTreeMap<_, i128>>::default()
                .with_apply_deltas([(t1.clone(), 1), (t1.clone(), -1)])
                .unwrap()
                .is_empty()
        );
```
