### Title
`CachedAccounts` token-balance cache re-seeds from stale `view` balance after `DefaultMap` zero-cleanup, double-counting balances - ([File: contracts/defuse/core/src/engine/state/cached.rs])

### Summary
`internal_add_balance` / `internal_sub_balance` in `CachedState` use `account.token_amounts.get(&token_id).is_none()` as a sentinel meaning "this token hasn't been touched in the cache yet, so seed it from the persisted `view` balance." Because `Amounts<HashMap<..>>` uses `DefaultMap::entry_or_default`, any entry that returns to `0` is silently removed from the map by `DefaultOccupiedEntry`/`DefaultVacantEntry`'s `Drop` impls. If a later operation in the same batch touches the same `(account, token)` again, the sentinel check sees `None` again and re-adds the *original, unmutated* `view.balance_of(...)`, double-counting the account's balance while the `TransferMatcher` only ever sums the raw per-call deltas that were actually passed to `add`/`sub`.

### Finding Description
The broken binding, stated as an equality that must hold and is violated:

`TransferMatcher`'s accumulated delta for `(account, token)` == the net change actually written into `CachedAccount.token_amounts` for `(account, token)`.

Code path:
- `internal_sub_balance` (`contracts/defuse/core/src/engine/state/cached.rs:226-255`) and `internal_add_balance` (`:200-224`) each do:
  ```
  if account.token_amounts.get(&token_id).is_none() {
      account.token_amounts.add(token_id.clone(), self.view.balance_of(owner_id, &token_id))...
  }
  account.token_amounts.sub/add(token_id, amount)...
  ``` [1](#0-0) 
- `Amounts::checked_apply` uses `self.0.entry_or_default(k)` [2](#0-1) 
- `DefaultMap::entry_or_default`'s returned entry wrapper deletes the map entry on `Drop` whenever the final value equals `Default::default()` (i.e. `0`), for both the vacant and occupied entry code paths: [3](#0-2) [4](#0-3) 

Root cause: the `get(&token_id).is_none()` check in `cached.rs` is meant to detect "have we cached this token's balance at all," but the automatic zero-cleanup means "balance reached exactly zero within this batch" is indistinguishable from "never touched." Once an in-batch operation drives a token's cached balance to zero (e.g. a full withdraw/spend), the entry disappears. A subsequent `internal_add_balance`/`internal_sub_balance` call for the *same* `(account, token)` later in the *same* `execute_intents`/`simulate_intents` batch will re-seed from `self.view.balance_of(...)` — the balance as persisted **before the batch started**, not `0`. This re-adds the pre-batch balance on top of the new delta, inflating the account's final cached (and eventually committed) balance.

Meanwhile, `Deltas::internal_add_balance`/`internal_sub_balance` (`contracts/defuse/core/src/engine/state/deltas.rs:136-164`) feed `TransferMatcher::deposit`/`withdraw` with the raw per-call `amount` arguments only — they never observe the re-seeding. So if a batch is constructed such that a signer's balance for a token is driven to exactly `0` and then increased again later in the same batch (e.g. mixing a full NEP-141 withdraw/spend of a token with a later NEP-245/NEP-171-triggered deposit back of the same token, matched by `TokenDiff`/`TransferMatcher` legs so the batch nets to zero and passes `finalize`), `TransferMatcher::finalize` reports the batch balanced (no `InvariantViolated`), while `CachedAccount.token_amounts` for that account/token ends up holding the pre-batch balance counted twice.

Why guards don't catch it: `TransferMatcher::finalize` only checks that summed deltas match transfers/deposits it was told about — it has no visibility into the `view.balance_of` re-seeding happening inside `CachedState`, so a batch that is internally balanced from the matcher's perspective can still corrupt the underlying cached/committed balance. There is no `checked_*` guard against re-seeding an already-drained entry, and no assertion that `token_amounts.get(...).is_none()` truly means "untouched this batch" versus "returned to zero this batch."

### Impact Explanation
This inflates the attacker's own `token_balances` entry for a token beyond what the Verifier actually custodies, matching the Critical category "a batch whose balance changes do not net to zero so the Verifier owes more than it custodies" / general protocol insolvency. The attacker can repeat this for any token they hold a balance in and for which they can construct a batch that (a) drains their cached balance for that token to exactly zero and (b) later in the same batch credits it again, while `TransferMatcher` still reports the batch as balanced. Each successful batch permanently inflates the on-chain `token_balances` record for that account/token without any matching asset entering the Verifier, and the inflated balance can later be withdrawn for real assets, draining the Verifier's actual custodied funds. This is repeatable per token/account and is attacker-profitable, not merely griefing.

### Likelihood Explanation
The attacker needs only: an existing non-zero balance in some token at the Verifier, and the ability to construct a `MultiPayload` batch with intents from allowed leg types (NEP-141/171/245, e.g. `token_diff` + `ft_withdraw`/`mt`/`nft` legs) that (1) drives their cached balance for a token to exactly zero mid-batch and (2) subsequently credits it again in the same batch via a matched transfer. This requires no privileged role, only ordinary `execute_intents`/`simulate_intents` access and signing with the attacker's own key — well within the stated unprivileged threat model. Cost is just gas/normal transaction fees; the exploit is repeatable across tokens and accounts the attacker controls.

### Recommendation
Do not rely on map presence/absence as the "already loaded from view" sentinel. Track per-account, per-batch "already synced from persisted state" using a separate `HashSet<TokenId>` (or similar) that is never cleared by `DefaultMap` zero-cleanup, so a token's view balance is seeded into the cache at most once per batch regardless of how many times its cached amount returns to zero in between.

### Proof of Concept
```rust
// cargo test -p defuse-core --lib engine::state::cached::tests::double_seed_after_zero_cleanup
#[test]
fn double_seed_after_zero_cleanup() {
    // Underlying view balance for (owner, token) = 100 (pre-batch persisted balance)
    // 1. internal_sub_balance(owner, [(token, 100)]) -> cache seeds 100, subtracts 100 -> 0
    //    Assert: CachedAccounts entry for (owner, token) is now absent (DefaultMap cleanup).
    // 2. internal_add_balance(owner, [(token, 100)]) -> get(&token).is_none() == true again
    //    -> re-seeds with view.balance_of == 100, then adds 100 -> cache now 200
    // 3. Assert TransferMatcher-visible net delta for (owner, token) across both calls == 0
    //    (100 withdrawn, 100 deposited, matcher reports balanced / finalize() == Ok)
    // 4. Assert final CachedAccount.token_amounts.amount_for(&token) == 200, NOT 100
    //    This proves committed balance (200) diverges from matcher's net delta (0) applied to
    //    the true starting balance (100): expected 100, actual 200 -> insolvency of 100 units.
}
```
The equality to assert on both sides: `matcher_net_delta(owner, token) == committed_balance(owner, token) - view_balance_before_batch(owner, token)`. Before the fix this is `0 == 100` (violated); after the fix (using a persistent "seeded" marker unaffected by zero-cleanup) it should hold as `0 == 0`.

### Citations

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

**File:** contracts/defuse/core/src/amounts.rs (L137-141)
```rust
    fn checked_apply(&mut self, k: T::K, f: impl FnOnce(T::V) -> Option<T::V>) -> Option<T::V> {
        let mut a = self.0.entry_or_default(k);
        *a = f(*a)?;
        Some(*a)
    }
```

**File:** crates/map-utils/src/cleanup.rs (L200-213)
```rust
impl<'a, E: 'a> Drop for DefaultVacantEntry<'a, E>
where
    E: VacantEntry<'a, V: Default + Eq>,
{
    #[inline]
    fn drop(&mut self) {
        let Some((v, entry)) = self.0.take() else {
            return;
        };
        if v != Default::default() {
            entry.insert(v);
        }
    }
}
```

**File:** crates/map-utils/src/cleanup.rs (L262-274)
```rust
impl<'a, E> Drop for DefaultOccupiedEntry<'a, E>
where
    E: OccupiedEntry<'a, V: Default + Eq>,
{
    #[inline]
    fn drop(&mut self) {
        let Some(entry) = self.0.take() else {
            return;
        };
        if entry.get() == &Default::default() {
            entry.remove();
        }
    }
```
