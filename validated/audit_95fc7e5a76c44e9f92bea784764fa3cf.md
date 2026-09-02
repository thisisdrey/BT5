### No vulnerability found for this question.

The fee actually collected on-chain in `TokenDiff::execute_intent` does not go through `closure_supply_delta` / `checked_mul_div_euclid` at all. The fee is computed directly as `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` applied to the raw signed delta's absolute value, for every negative leg, and added to `fees_collected` [1](#0-0) . This is exactly the binding the question requires (`Pips::fee_ceil` on the raw delta), so both sides of the equality are trivially identical — there is no round trip through `supply_delta`/`closure_supply_delta` in this path.

The `closure`, `closure_many`, `closure_deltas`, and `closure_delta` functions that do use `checked_mul_div_ceil` (in `supply_delta`) and `checked_mul_div_euclid` (in `closure_supply_delta`) are a separate helper API [2](#0-1) . Searching the repository shows these closure-related functions are referenced only within `contracts/defuse/core/src/intents/token_diff.rs` itself and its test module [3](#0-2) ; they are not invoked by `execute_intent` or any other on-chain execution path that moves funds or collects fees. Since no reachable attacker-controlled call path routes fee collection through `closure_supply_delta`, any rounding divergence between `checked_mul_div_ceil` and `checked_mul_div_euclid` in these helper functions cannot cause actual fee under-collection in the Verifier.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-78)
```rust
            // take fees only from negative deltas (i.e. token_in)
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L120-204)
```rust
impl TokenDiff {
    /// Returns [`TokenDiff`] closure to successfully execute `self`
    /// assuming given `fee`
    #[inline]
    pub fn closure(self, fee: Pips) -> Option<TokenDeltas> {
        Self::closure_deltas(self.diff.into_inner(), fee)
    }

    /// Returns [`TokenDiff`] closure to successfully execute given set
    /// of distinct [`TokenDiff`] assuming given `fee`
    #[inline]
    pub fn closure_many(diffs: impl IntoIterator<Item = Self>, fee: Pips) -> Option<TokenDeltas> {
        Self::closure_deltas(diffs.into_iter().flat_map(|d| d.diff.into_inner()), fee)
    }

    /// Returns closure for deltas that should be given in a single
    /// [`TokenDiff`] to successfully execute given set of distinct `deltas`
    /// assuming given `fee`
    #[inline]
    pub fn closure_deltas(
        deltas: impl IntoIterator<Item = (TokenId, i128)>,
        fee: Pips,
    ) -> Option<TokenDeltas> {
        deltas
            .into_iter()
            // collect total supply deltas
            .try_fold(TokenDeltas::default(), |deltas, (token_id, delta)| {
                let supply_delta = Self::supply_delta(&token_id, delta, fee)?;
                deltas.with_apply_delta(token_id, supply_delta)
            })?
            .into_inner()
            .into_iter()
            // calculate closures from total supply deltas
            .try_fold(TokenDeltas::default(), |deltas, (token_id, delta)| {
                let closure = Self::closure_supply_delta(&token_id, delta, fee)?;
                deltas.with_apply_delta(token_id, closure)
            })
    }

    /// Returns closure for delta that should be given in a single
    /// [`TokenDiff`] to successfully execute [`TokenDiff`] with given
    /// `delta` on the same token assuming given `fee`.
    #[inline]
    pub fn closure_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        Self::closure_supply_delta(token_id, Self::supply_delta(token_id, delta, fee)?, fee)
    }

    /// Returns total supply delta from token delta
    #[inline]
    fn supply_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        if delta < 0 {
            // fee is taken only on negative deltas (i.e. token_in)
            delta.checked_mul_div_ceil(
                Self::token_fee(token_id, delta.unsigned_abs(), fee)
                    .invert()
                    .as_pips()
                    .into(),
                Pips::MAX.as_pips().into(),
            )
        } else {
            // token_out
            Some(delta)
        }
    }

    /// Returns closure for total supply delta that should be given in
    /// a single [`TokenDiff`] to successfully execute [`TokenDiff`] with
    /// given `delta` on the same token assuming given `fee`.
    #[inline]
    pub fn closure_supply_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        let closure = delta.checked_neg()?;
        if closure < 0 {
            // fee is taken only on negative deltas (i.e. token_in)
            closure.checked_mul_div_euclid(
                Pips::MAX.as_pips().into(),
                Self::token_fee(token_id, delta.unsigned_abs(), fee)
                    .invert()
                    .as_pips()
                    .into(),
            )
        } else {
            // token_out
            Some(closure)
        }
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L1-1)
```rust
#![allow(clippy::cloned_ref_to_slice_refs)]
```
