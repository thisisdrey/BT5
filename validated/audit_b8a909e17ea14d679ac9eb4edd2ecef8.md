### Title
Protocol fee on `Nep245`/`Imt` token diffs can be bypassed by splitting one negative delta into multiple unit-sized `TokenDiff` intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::execute_intent` computes the fee for a negative delta on a per-intent basis using `TokenDiff::token_fee`, which returns `Pips::ZERO` for `Nep245`/`Imt` tokens whenever the intent-local `|delta|` is `<= 1`. Because fee eligibility is evaluated per individual `TokenDiff` intent rather than on the signer's net delta for that token across the whole `MultiPayload`, a signer can split a single fee-eligible `delta = -2` into two intents each with `delta = -1` and pay zero fee instead of `Pips::fee_ceil(protocol_fee, 2)`.

### Finding Description
The binding that should hold is:
`fee_collected_for(token T, signer S, batch B) == Pips::fee_ceil(protocol_fee, |net_negative_delta_of_T_by_S_in_B|)`

In `TokenDiff::execute_intent`, for each `(token_id, delta)` pair in a single intent's diff map: [1](#0-0) 
the fee is computed strictly from that one intent's `amount = delta.unsigned_abs()`, via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`.

`TokenDiff::token_fee` classifies `Nep245`/`Imt` tokens as fee-exempt whenever `amount <= 1`: [2](#0-1) 

Because the fee decision is scoped to the `amount` inside a single intent's diff entry — not the signer's aggregate delta for that token across the batch — a signer holding `>=2` units of a `Nep245`/`Imt` token can sign two separate `TokenDiff` intents, each with `delta = -1` for token `T` (each below the `amount > 1` threshold, so `token_fee` returns `Pips::ZERO` and `fee_ceil` = 0), balanced by any positive legs elsewhere in the same `MultiPayload` so the batch nets to zero. The `MultiPayload`/`Engine` executes both intents independently — there is no aggregation of per-token deltas across intents before the fee decision is made, and no code path (`verify`, nonce checks, `TransferMatcher::finalize`, etc.) recombines split legs of the same token before `token_fee` is invoked. Issuing the equivalent single intent with `delta = -2` would instead route through the `amount > 1` branch, applying the nonzero `protocol_fee` and yielding `fee_ceil(protocol_fee, 2) > 0` for typical fee configurations (e.g. `Pips::ONE_PERCENT`).

### Impact Explanation
The `fee_collector` (`engine.state.fee_collector()`) receives strictly less than the fee owed on the signer's actual net token-in amount for a `Nep245`/`Imt` token, purely because the signer chose to submit the diff as multiple unit-sized intents instead of one. This is a protocol fee under-collection/bypass on `Nep245`/`Imt` legs whenever the "real" negative amount would be `> 1` but is decomposed into legs each `<= 1`. It is repeatable by any signer, for any `Nep245`/`Imt` token they hold in quantity `>= 2`, across any number of batches, and scales with the amount split (larger true amounts can be fully decomposed into unit legs to always pay zero fee on that token type).

### Likelihood Explanation
Preconditions are trivial for any unprivileged attacker: hold `>= 2` units of any `Nep245`/`Imt` token in the Verifier, and have `protocol_fee > 0` configured. The attacker only needs to sign two intents instead of one — no special privileges, roles, or counterparties beyond whatever is needed to balance the batch (which they can supply themselves via a compensating positive leg, e.g. from another intent or their own account) are required.

### Recommendation
Compute `TokenDiff` fee eligibility and fee amount from the signer's net negative delta per token aggregated across all `TokenDiff` intents in the same execution/`MultiPayload`, not per individual intent's local diff entry — e.g. accumulate per-signer, per-token deltas before applying `token_fee`/`fee_ceil`, or otherwise make the `amount > 1` threshold decision based on the total negative exposure for that token within the batch rather than the value in a single intent.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or a near-workspaces sandbox test under tests/src/tests/defuse/intents/token_diff.rs)
//
// Setup: signer holds 2 units of Nep245 token T (multi-token quantity 2),
// protocol_fee = Pips::ONE_PERCENT, counterparty leg balances the batch to net zero.
//
// Case A (single intent): TokenDiff { diff: {T: -2, U: +k} }
//   -> fees_collected[T] == Pips::ONE_PERCENT.fee_ceil(2)  // > 0
//
// Case B (split intents): two TokenDiff intents from the SAME signer in ONE MultiPayload:
//   intent1: TokenDiff { diff: {T: -1, U1: +k1} }
//   intent2: TokenDiff { diff: {T: -1, U2: +k2} }
//   -> fees_collected[T] across both intents == 0 (each hits token_fee's amount<=1 exemption)
//
// Assertion demonstrating the bypass:
assert_eq!(
    Pips::ONE_PERCENT.fee_ceil(2),
    total_fees_collected_case_a_for_T
);
assert_eq!(0, total_fees_collected_case_b_for_T);
assert_ne!(
    total_fees_collected_case_a_for_T,
    total_fees_collected_case_b_for_T
); // fee bypassed by splitting
```

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
