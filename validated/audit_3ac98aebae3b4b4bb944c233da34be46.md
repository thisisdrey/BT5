Based on tracing the code, the premise of this question doesn't hold up — the magnitude computation is not "independently" done twice in production code; it's computed once and passed through.

**The actual code path:**

1. `TokenDiff::execute_intent` calls `engine.state.internal_apply_deltas(signer_id, [(token_id, *delta)])` at [1](#0-0) .

2. `engine.state` is `Deltas<S>` [2](#0-1) . `Deltas<S>` does **not** override `internal_apply_deltas`, so it uses the default trait implementation on `State`, which computes `delta.unsigned_abs()` exactly once and dispatches to `self.internal_sub_balance`/`self.internal_add_balance` with that single `u128` amount value: [3](#0-2) .

3. Because `self` here is `Deltas<S>`, this call resolves to `Deltas`'s own overridden `internal_add_balance`/`internal_sub_balance`, which take that same `amount: u128` value, forward it to the inner state for the actual balance mutation, AND pass the **identical value** to `self.deltas.deposit(...)`/`self.deltas.withdraw(...)` (i.e. `TokenTransferMatcher` bookkeeping) in the same loop iteration: [4](#0-3) .

There is only **one** computation of `unsigned_abs()` per delta (in the default `internal_apply_deltas`), and that single `u128` value is threaded through to both the balance-storage write and the `TransferMatcher` bookkeeping call. `TokenTransferMatcher::add_delta` (which does its own separate `delta.unsigned_abs()`) is a distinct helper method used only in the `#[cfg(test)]` module (`test_transfers`, `test_unmatched`) [5](#0-4)  — it is never called from the `TokenDiff` execution path, so its independent `unsigned_abs()` computation is never in a position to diverge from the balance-mutation path in production.

Even setting that aside, `i128::unsigned_abs()` is a pure, deterministic standard-library function: for a fixed input `delta`, it always returns the same `u128` output regardless of how many times or where it's called. There's no non-determinism, no wrapping, no truncation between `i128::MIN.unsigned_abs() == 170141183460469231731687303715884105728u128` computed in one call site versus another — this is guaranteed by Rust language semantics, not by any application-level invariant that could be violated.

The `i128::MIN` boundary case is correctly rejected further downstream because the resulting `u128` magnitude (`2^127`) will exceed any realistic `Amounts` balance, causing `checked_sub`/`checked_add` in `Amounts::sub`/`add` to return `None`, propagating `DefuseError::BalanceOverflow` [6](#0-5) , which aborts the whole intent atomically (both the balance mutation and the `TransferMatcher` update happen in the same fallible loop iteration, so no partial state is committed).

#No vulnerability found for this question.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L64-67)
```rust
            // add delta to signer's account
            engine
                .state
                .internal_apply_deltas(signer_id, [(token_id.clone(), *delta)])?;
```

**File:** contracts/defuse/core/src/engine/mod.rs (L14-17)
```rust
pub struct Engine<S, I> {
    pub state: Deltas<S>,
    pub inspector: I,
}
```

**File:** contracts/defuse/core/src/engine/state/mod.rs (L91-105)
```rust
    fn internal_apply_deltas(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, i128)>,
    ) -> Result<()> {
        for (token_id, delta) in tokens {
            let tokens = [(token_id, delta.unsigned_abs())];
            if delta.is_negative() {
                self.internal_sub_balance(owner_id, tokens)?;
            } else {
                self.internal_add_balance(owner_id.to_owned(), tokens)?;
            }
        }
        Ok(())
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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L497-537)
```rust
    #[test]
    fn test_transfers() {
        let mut transfers = TransferMatcher::default();
        let [a, b, c, d, e, f, g]: [AccountId; 7] =
            ["a", "b", "c", "d", "e", "f", "g"].map(|s| format!("{s}.near").parse().unwrap());
        let [ft1, ft2] = ["ft1", "ft2"].map(|a| {
            TokenId::from(Nep141TokenId::new(
                format!("{a}.near").parse::<AccountId>().unwrap(),
            ))
        });

        let deltas: HashMap<AccountId, TokenDeltas> = [
            (&a, [(&ft1, -5), (&ft2, 1)].as_slice()),
            (&b, [(&ft1, 4), (&ft2, -1)].as_slice()),
            (&c, [(&ft1, 3)].as_slice()),
            (&d, [(&ft1, -10)].as_slice()),
            (&e, [(&ft1, -1)].as_slice()),
            (&f, [(&ft1, 10)].as_slice()),
            (&g, [(&ft1, -1)].as_slice()),
        ]
        .into_iter()
        .map(|(owner_id, deltas)| {
            (
                owner_id.clone(),
                TokenDeltas::default()
                    .with_apply_deltas(
                        deltas
                            .iter()
                            .map(|(token_id, delta)| ((*token_id).clone(), *delta)),
                    )
                    .unwrap(),
            )
        })
        .collect();

        for (owner, (token_id, delta)) in deltas
            .iter()
            .flat_map(|(owner_id, deltas)| iter::repeat(owner_id).zip(deltas))
        {
            assert!(transfers.add_delta(owner.clone(), token_id.clone(), *delta));
        }
```

**File:** contracts/defuse/core/src/amounts.rs (L77-82)
```rust
    pub fn sub(&mut self, k: T::K, amount: u128) -> Option<T::V>
    where
        T::V: CheckedSub<u128>,
    {
        self.checked_apply(k, |a| a.checked_sub(amount))
    }
```
