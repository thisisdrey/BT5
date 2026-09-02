### Title
Fee bypass on NEP-245/IMT `TokenDiff` via splitting a multi-unit withdrawal into multiple `delta == -1` intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives fees on `TokenIdType::Nep245`/`TokenIdType::Imt` only when the *per-intent* `|delta|` is `<= 1`. Because this threshold is evaluated independently for each `TokenDiff::execute_intent` call rather than on the signer's aggregate negative delta for that token across the batch, a signer can split any multi-unit MT/IMT withdrawal into several single-unit (`delta == -1`) `TokenDiff` intents (each under its own nonce) and pay zero protocol fee where a single equivalent intent would have paid a non-zero fee.

### Finding Description
The broken binding is:

`fee_collector_credit(token T, batch) == fee_ceil(protocol_fee, Σ|negative deltas of T for signer across batch|)`

but the code actually computes:

`fee_collector_credit(token T, batch) == Σ over each TokenDiff intent i of fee_ceil(TokenDiff::token_fee(T, |delta_i|, protocol_fee), |delta_i|)`

`token_fee` explicitly zeroes the fee per-call when `amount <= 1` for NEP-245/IMT tokens: [1](#0-0) 

and `execute_intent` invokes this per `(token_id, delta)` pair within a single `TokenDiff.diff`, with no visibility into any other `TokenDiff` intent in the same batch: [2](#0-1) 

Each `TokenDiff::execute_intent` call independently calls `internal_apply_deltas` and, for negative deltas, `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, then adds the (possibly zero) fee to `fees_collected`, which is credited to `engine.state.fee_collector()` at the end of that single intent's execution: [3](#0-2) 

Nothing in the engine aggregates deltas across multiple `TokenDiff` intents in the same `execute_intents`/`simulate_intents` batch before deciding the fee; aggregation only happens afterward in `TransferMatcher`/`Deltas::finalize`, which is solely concerned with conservation of value (deposits vs. withdrawals matching to produce `Transfers`), not with fee accounting: [4](#0-3) 

**Attacker's exact payload / call sequence**: signer holds `>= 2` of a NEP-245 `token_id` T. Instead of signing one `MultiPayload` with a `TokenDiff{ diff: {T: -2}, ... }` (which would evaluate `token_fee(T, 2, fee)` → non-zero `fee`), the signer signs two separate `TokenDiff` intents (`{T: -1}` each) under two distinct nonces, packaged in the same batch handed to `execute_intents`/`simulate_intents`, alongside whatever counterparty intent(s) supply the matching `+2` (or `+1`/`+1`) positive delta needed to satisfy `TransferMatcher::finalize`'s net-zero requirement (positive deltas never incur a fee regardless of amount, so the counterparty side is unaffected). Each of the two `-1` intents independently hits the `amount <= 1` branch of `token_fee` and contributes `Pips::ZERO`, i.e., `fee_ceil(0, 1) == 0`.

**Why existing guards don't help**: `MultiPayload::verify`, nonce/signature checks, and `TransferMatcher::finalize`'s invariant only enforce that value moved nets to zero across the batch and that signatures/nonces are valid — none of them recompute or cap fees based on the signer's aggregate delta per token. `token_fee`'s comment ("do not take fees on NFTs and MTs with `|delta| <= 1`") reflects a per-call design intent to avoid fee rounding oddities on unit transfers, but it has no defense against a signer decomposing a larger withdrawal into unit-sized intents.

### Impact Explanation
For any NEP-245 (MT) or IMT token, a signer can withdraw/trade an arbitrary quantity of that token while paying zero protocol fee, simply by expressing the withdrawal as N separate `delta == -1` `TokenDiff` intents instead of one `delta == -N` intent. This is a direct, repeatable "protocol fees bypassed" condition (Critical category per the audit's impact list): value that should have been credited to `fee_collector` is never collected. It's repeatable per account, per token, and per batch — the attacker (or a colluding relayer/solver pair) can always resubmit their trade in unit-sized chunks with no additional cost besides more nonces/gas.

### Likelihood Explanation
Preconditions are trivial and fully within an unprivileged signer's control: only a positive balance of an NEP-245/IMT token (`>= 2` units) and a counterparty willing to close the trade (which is required for any legitimate NEP-245 diff trade anyway, fee or not). No special role, relayer key, or DAO permission is required — this is an ordinary `execute_intents`/`simulate_intents` call with a normal `MultiPayload` batch. The only "cost" to the attacker is signing/including more intents (more nonces), which is cheap and fully under attacker control. This makes the bypass highly feasible and repeatable for every MT/IMT-denominated trade.

### Recommendation
Compute the NEP-245/IMT fee threshold against the signer's *aggregate* negative delta for a given `token_id` across the whole intents batch (e.g., by having the engine pre-aggregate deltas per `(signer_id, token_id)` before invoking `token_fee`, or by moving the `amount <= 1` waiver to be evaluated once per signer/token pair at `finalize`-time rather than per individual `TokenDiff` intent).

### Proof of Concept
`cargo test` in the `defuse` core/tests crate, comparable to the existing suite in `tests/src/tests/defuse/intents/token_diff.rs`:
1. Set up `env` with `fee = Pips::ONE_PERCENT` (nonzero) and an NEP-245 token `T` with a signer holding balance `>= 2` of `T`, plus a counterparty able to provide the matching `+2` delta.
2. **Case A (single intent)**: sign one `TokenDiff{ diff: {T: -2}, ... }` from the signer plus the counterparty's matching intent; execute via `execute_intents`; assert `fee_collector` balance of `T` increases by `TokenDiff::token_fee(T, 2, fee).fee_ceil(2)` (non-zero).
3. **Case B (split intents)**: sign two separate `TokenDiff{ diff: {T: -1}, ... }` intents (distinct nonces) from the same signer plus the counterparty's matching intent(s); execute via `execute_intents`; assert `fee_collector` balance of `T` increases by `0`.
4. Assert the two `fee_collector` deltas differ (Case A > 0, Case B == 0) despite both moving the identical net `-2`/`+2` of `T`, proving the fee-bypass binding violation.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-78)
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
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
        }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-216)
```rust
    #[inline]
    pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
        let token_id = token_id.into();
        match token_id {
            TokenIdType::Nep141 => {}
            TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
            // do not take fees on NFTs and MTs with |delta| <= 1
            TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
        }
        fee
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L266-283)
```rust
    // If unmatched deltas overflow, then Err(None) is returned.
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
