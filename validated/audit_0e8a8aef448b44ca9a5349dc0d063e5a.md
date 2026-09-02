### Title
Protocol fee on NEP-245/IMT `TokenDiff` legs bypassed by splitting a single large delta into unit (`|delta| == 1`) legs - (contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` exempts NEP-245/IMT token legs from fees whenever the *per-intent* delta magnitude is `<= 1`, and `TokenDiff::execute_intent` computes and collects the fee independently for each `TokenDiff` intent in the batch. An attacker can replace one `TokenDiff{delta: -N}` leg with `N` separate `TokenDiff{delta: -1}` intents on the same NEP-245/IMT `token_id`, each of which hits the `amount > 1` exemption and returns `Pips::ZERO`, so the aggregate fee collected across the batch is `0` instead of `Pips::fee_ceil(N)`.

### Finding Description
The binding that should hold is:
`fee_collected(TokenDiff{T: -N}) == fee_collected({TokenDiff{T: -1}} × N)` for the same signer, token `T` of type `Nep245`/`Imt`, and `protocol_fee`.

In `TokenDiff::execute_intent` [1](#0-0) , for each `(token_id, delta)` pair in `self.diff`, the fee is computed as `Self::token_fee(token_id, delta.unsigned_abs(), protocol_fee).fee_ceil(amount)` using only that single intent's own delta magnitude.

`TokenDiff::token_fee` exempts `Nep245`/`Imt` tokens whenever the per-call `amount <= 1`: [2](#0-1) 

For a single `TokenDiff{T: -N}` with `N > 1`, `amount = N > 1`, so the `Nep245 | Imt if amount > 1 => {}` arm falls through to `fee`, and `fee_ceil(N)` is charged and credited to the fee collector via `internal_add_balance`.

If the attacker instead signs `N` separate `TokenDiff` intents, each with `diff = {T: -1}`, and batches them (with a counterparty's `TokenDiff{T: +N, other: -M}` needed to satisfy `TransferMatcher::finalize`'s zero-sum invariant per `contracts/defuse/core/src/engine/state/deltas.rs`), then for each of the `N` intents `execute_intent` calls `token_fee(T, 1, protocol_fee)`, which matches the exclusion arm `TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO`, returning `Pips::ZERO` every time. `fees_collected` is `Pips::ZERO.fee_ceil(1) == 0` for every leg, so total fee collected across the whole batch is `0`, strictly less than `Pips::fee_ceil(N)`.

Existing invariants do not prevent this: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize` only enforce that token balance deltas net to zero across all accounts in the batch — they do not constrain how a single account's net negative delta on one token is partitioned across multiple signed intents, and fee computation has no cross-intent, per-signer, per-token aggregation.

### Impact Explanation
Protocol fees on NEP-245/IMT (multi-token) transfers can be reduced to zero for arbitrarily large amounts by splitting the transfer into unit-sized `TokenDiff` legs, at the cost of one extra signed intent per unit of amount. This directly matches the "protocol fees bypassed" Critical category: the fee collector's account is credited less than it is owed for the exact same net economic transfer, for every NEP-245/IMT trade the attacker chooses to structure this way. The attack is repeatable across any signer, any NEP-245/IMT `token_id`, and any batch.

### Likelihood Explanation
No special privileges are required — any signer who can call `execute_intents`/`simulate_intents` with a self-crafted `MultiPayload` can do this. The only cost is needing to sign `N` intents instead of `1` (bounded by gas/message-size practicality for the attacker, not by any protocol control), and needing a counterparty leg (which can remain a single, unsplit `TokenDiff`) to satisfy the zero-sum invariant. This is straightforward and fully within reach of an unprivileged attacker acting as either side of a self-arranged trade.

### Recommendation
Compute the NEP-245/IMT fee exemption based on the aggregate net negative delta per `(signer_id, token_id)` across the whole batch (e.g., accumulate deltas per token across all intents before invoking `token_fee`), not per individual `TokenDiff` intent's local delta magnitude. Alternatively, remove the amount-based exemption entirely and always charge `fee` for `Nep245`/`Imt`, or perform the `amount > 1` check against a batch-wide/pre-aggregated value.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or `tests/src/tests/defuse/intents/token_diff.rs` sandbox test), asserting on both sides of the binding:
1. Build a signer with `N` units (e.g. `N = 10`) of a `Nep245`/IMT token id `T`, and a counterparty holding sufficient balance of another token `other`.
2. Case A: sign one `TokenDiff{diff: {T: -N, other: +closure}}` from signer, plus counterparty's matching `TokenDiff{T: +N, other: -M}`; execute via `Engine::execute_signed_intents`; read `TokenDiffEvent::fees_collected` (or fee_collector's post-balance of `T`) — assert `fees_collected.amount_for(&T) == Pips::fee_ceil(protocol_fee, N)` (nonzero for `protocol_fee > 0`).
3. Case B: sign `N` separate `TokenDiff{diff: {T: -1}}` intents from the same signer (plus the same counterparty intent(s) to balance the batch and one more intent from signer receiving `other`); execute the whole `MultiPayload` batch; assert `fees_collected` summed across all `N` `TokenDiffEvent`s for token `T` equals `0`.
4. Assert `fee_collector`'s balance increase for `T` in Case A `> 0` while in Case B `== 0`, demonstrating `sum(Pips::ZERO * N) == 0 < Pips::fee_ceil(N)`.

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
