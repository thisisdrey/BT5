### Title
`internal_add_balance`/`internal_sub_balance` re-seed a wiped zero-balance entry from stale on-chain state, letting the cached balance diverge from `TransferMatcher`'s net-zero deltas - ([File: contracts/defuse/core/src/engine/state/cached.rs])

### Summary
`CachedState::internal_add_balance`/`internal_sub_balance` in `contracts/defuse/core/src/engine/state/cached.rs` lazily seed `account.token_amounts` from `self.view.balance_of(...)` only when the entry is absent. Because `Amounts`/`DefaultMap` (`crates/map-utils/src/cleanup.rs`, `contracts/defuse/core/src/amounts.rs`) automatically removes an entry the instant its value returns to `0`, a balance that is legitimately fully spent and then touched again later in the same batch gets silently re-seeded with the *original* on-chain balance, duplicating it. `TransferMatcher` (in `contracts/defuse/core/src/engine/state/deltas.rs`), which tracks deltas independently from the raw `amount` arguments (not from `token_amounts`), never sees this duplication and can still `finalize()` cleanly, so the invariant "matcher delta == actual balance change" breaks silently.

### Finding Description
The binding that must hold is:
`matcher_delta(account, token)` (accumulated in `TransferMatcher` via `Deltas::internal_add_balance`/`internal_sub_balance`, `contracts/defuse/core/src/engine/state/deltas.rs:136-164`) `== actual_change(account, token)` (the net change applied to `CachedAccount::token_amounts` in `contracts/defuse/core/src/engine/state/cached.rs:200-255`, which is what eventually becomes the persisted `token_balances`).

`CachedState::internal_add_balance`/`internal_sub_balance` (`contracts/defuse/core/src/engine/state/cached.rs:200-255`) do:
```
if account.token_amounts.get(&token_id).is_none() {
    account.token_amounts.add(token_id.clone(), self.view.balance_of(&owner_id, &token_id))...;
}
account.token_amounts.add/sub(token_id, amount)...;
```
`token_amounts` stores the account's *absolute* cached balance per token, seeded once from the underlying, uncached `view.balance_of` (the pre-transaction on-chain value) the first time the token is touched in this execution.

`Amounts::checked_apply` (`contracts/defuse/core/src/amounts.rs:137-141`) uses `DefaultMap::entry_or_default`, whose `Drop` impl (`crates/map-utils/src/cleanup.rs:262-275`) removes the map entry the moment its value equals `Default::default()` (`0u128`). This means: if, within one `execute_intents` batch, a sequence of ops drives an account's cached balance for a token exactly back to `0`, the entry is deleted from `token_amounts` — indistinguishable from "never touched."

If a *later* intent in the same batch touches `(owner_id, token_id)` again, `account.token_amounts.get(&token_id).is_none()` is true again, so the code re-seeds the entry from `self.view.balance_of(...)`. But `self.view` is the immutable, pre-transaction snapshot — it still returns the *original* balance that was already spent earlier in this same batch. That original amount gets re-added to the cache, effectively minting a duplicate of the already-consumed balance.

Meanwhile `Deltas::internal_add_balance`/`internal_sub_balance` (`contracts/defuse/core/src/engine/state/deltas.rs:136-164`) record `self.deltas.deposit(...)`/`.withdraw(...)` using only the raw `amount` argument passed by the caller (from `TokenDiff::execute_intent`, `contracts/defuse/core/src/intents/token_diff.rs:59-79`), completely independent of `token_amounts`. `TransferMatcher::finalize` (`contracts/defuse/core/src/engine/state/deltas.rs:267-283`) only checks that these raw deltas net to zero across counterparties — it has no visibility into the CachedState-side re-seeding bug, so a batch can `finalize()` successfully while the cached `token_amounts` (and hence the persisted `token_balances`) is inflated.

Concrete construction (attacker signs both counterparties, using accounts they control): attacker account `A` has an existing on-chain balance of `100` of token `X`. In one `execute_intents(signed)` call, submit a `MultiPayload` batch containing:
1. Intent 1 (signed by `A`): `TokenDiff{diff: {X: -100}}` matched against a counterparty `B` (also attacker-controlled) with `TokenDiff{diff: {X: +100, Y: -100}}` etc. — this drives `A`'s cached `token_amounts[X]` from `100` to `0`, which the `DefaultMap` cleanup removes from the map.
2. Intent 2 (signed by `A`), later in the same batch: any further op on token `X` for `A` (e.g. another small `TokenDiff` leg or a mint/notify path routed through `internal_add_balance`) causes `token_amounts.get(&X).is_none()` to be true again, so `self.view.balance_of(A, X)` (still `100`, since it reads pre-transaction state) is re-added before applying the new delta — inflating `A`'s cached balance by the already-spent `100`.

`TransferMatcher::finalize` only enforces zero-sum on the raw deltas the attacker chose to submit for intent 1 and intent 2, which the attacker fully controls and can balance to zero across their own counterparty accounts, so it does not detect or prevent the extra `100` silently injected into `token_amounts`.

None of the listed guards (`MultiPayload::verify`, nonce/salt checks, `Lock::get_mut`, `assert_one_yocto`, role guards) inspect `token_amounts` re-seeding logic; they authenticate signatures/nonces/locks, not balance bookkeeping consistency.

### Impact Explanation
This allows an unprivileged attacker to mint value out of thin air in their own cached `token_balances` for any token they already hold a balance of, without any counterparty loss exceeding what they choose to net to zero via `TransferMatcher`. Once committed, `sum(token_balances)` exceeds assets actually custodied by the Verifier — this is the Critical "protocol insolvency" impact category. The attack is repeatable per token per batch and scales with the attacker's own existing balance (the amount duplicated equals whatever the attacker's on-chain balance for that token was before the batch), and can be chained/repeated across batches to keep inflating the seeded balance.

### Likelihood Explanation
This requires the attacker to hold a balance of some token, and be able to craft a batch with at least two touches of the same `(account, token)` pair where the intermediate cached balance transiently returns to exactly zero. Since the attacker fully controls the `TokenDiff` deltas, referenced accounts, and intent ordering within a `MultiPayload` batch, and this is exactly the kind of test surface described ("Probe the interaction between `entry_or_default`, the zero-value cleanup, and iteration order during finalize"), this is straightforward to construct and does not require any privileged role, relayer key, or third-party cooperation beyond the attacker's own signed intents.

### Recommendation
Do not use presence/absence in `token_amounts` as a proxy for "has this token been seeded from view this execution." Instead, track seeded/touched tokens with a separate `HashSet<TokenId>` (or a wrapper type that distinguishes "cached value of 0" from "not yet cached") so that a balance reaching zero mid-batch is not conflated with "never touched," and a later touch does not re-pull `self.view.balance_of`.

### Proof of Concept
```rust
// contracts/defuse/core/src/engine/state/cached.rs (new test)
#[test]
fn zero_balance_cleanup_causes_double_credit() {
    // Arrange a StateView mock whose `balance_of(A, X)` always returns 100
    // (simulating pre-transaction persisted balance).
    let view = MockView::new().with_balance("A", "X", 100);
    let mut cached = CachedState::new(view);

    // Step 1: spend the full balance to zero -> triggers DefaultMap cleanup,
    // removing the (A, X) entry from `token_amounts`.
    cached.internal_sub_balance("A".parse().unwrap(), [(token_x(), 100)]).unwrap();
    assert_eq!(cached.balance_of(&"A".parse().unwrap(), &token_x()), 0);

    // Step 2: touch (A, X) again later in the same execution with a small add.
    cached.internal_add_balance("A".parse().unwrap(), [(token_x(), 5)]).unwrap();

    // Assert on both sides of the binding:
    // matcher_delta(A, X) as tracked externally (sum of raw amounts): -100 + 5 = -95
    // actual_change(A, X) applied to cached token_amounts: expected 0 - 100 + 5 = -95
    // BUG: actual cached balance is view.balance_of(100) [re-seeded] + 5 = 105,
    // i.e. actual_change = +5, NOT -95 -> divergence of 100.
    assert_eq!(cached.balance_of(&"A".parse().unwrap(), &token_x()), 5); // fails: yields 105
}
```
Run with `cargo test -p defuse-core zero_balance_cleanup_causes_double_credit`. A full end-to-end reproduction should additionally run this through `Deltas<CachedState<...>>` with two `TokenDiff` intents inside one `execute_intents(signed)` `MultiPayload` batch (both counterparties controlled by the attacker) in a `near-workspaces` sandbox, asserting that `TransferMatcher::finalize()` succeeds (net-zero deltas as chosen by the attacker) while the resulting `token_balances` for the attacker's account exceeds what was deposited, demonstrating Verifier insolvency. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** contracts/defuse/core/src/engine/state/cached.rs (L200-224)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        token_amounts: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let account = self
            .accounts
            .get_or_create(owner_id.clone(), |owner_id| {
                self.view.is_account_locked(owner_id)
            })
            .as_inner_unchecked_mut();
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
        }
        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L226-255)
```rust
    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        token_amounts: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let account = self
            .accounts
            .get_or_create(owner_id.to_owned(), |owner_id| {
                self.view.is_account_locked(owner_id)
            })
            .get_mut()
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;
        for (token_id, amount) in token_amounts {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

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
        }
        Ok(())
    }
```

**File:** contracts/defuse/core/src/amounts.rs (L137-141)
```rust
    fn checked_apply(&mut self, k: T::K, f: impl FnOnce(T::V) -> Option<T::V>) -> Option<T::V> {
        let mut a = self.0.entry_or_default(k);
        *a = f(*a)?;
        Some(*a)
    }
```

**File:** crates/map-utils/src/cleanup.rs (L262-275)
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
}
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L136-164)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_add_balance(owner_id.clone(), [(token_id.clone(), amount)])?;
            if !self.deltas.deposit(owner_id.clone(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }

    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_sub_balance(owner_id, [(token_id.clone(), amount)])?;
            if !self.deltas.withdraw(owner_id.to_owned(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L267-283)
```rust
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-79)
```rust
        for (token_id, delta) in &self.diff {
            if *delta == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            // add delta to signer's account
            engine
                .state
                .internal_apply_deltas(signer_id, [(token_id.clone(), *delta)])?;

            // take fees only from negative deltas (i.e. token_in)
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
        }
```
