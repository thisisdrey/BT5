### Title
Nep245/Imt protocol fee bypass by splitting a single TokenDiff into N unit-delta TokenDiff intents in one batch - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` tokens whenever the per-intent `amount <= 1`, and this check is applied independently per `TokenDiff` intent rather than on the aggregate amount moved for a given `TokenId` across a batch. Because a single `DefuseIntents` payload can carry an arbitrary `Vec<Intent>` under one signature, an attacker can split a 10,000-unit `token_in` into 10,000 `TokenDiff` intents each with `delta = -1`, causing `fee_ceil` to evaluate to `Pips::ZERO` on every leg and totally avoiding the fee that a single undivided `TokenDiff{diff:{token_id:-10000}}` would have paid.

### Finding Description
The intended binding is: for a given `token_id` and total negative amount moved `A` within one settlement, `fees_collected.get(token_id) == Pips::fee_ceil(fee, A)`. The code instead computes and collects fee independently per `TokenDiff` intent: [1](#0-0) 

and `token_fee` special-cases `Nep245`/`Imt` token types to return `Pips::ZERO` whenever the delta magnitude for *that intent* is `<= 1`: [2](#0-1) 

Because a `DefuseIntents` payload holds a `Vec<Intent>` executed sequentially against the same signer/engine state under a single signature/nonce, the attacker can sign one `MultiPayload` containing 10,000 `Intent::TokenDiff` entries, each `{diff:{token_id: -1}}`, instead of one `{diff:{token_id:-10000}}`: [3](#0-2) [4](#0-3) 

For the undivided case, `fee = Pips::ONE_PERCENT.fee_ceil(10_000) = 100`, collected once. For the split case, each of the 10,000 legs calls `token_fee(token_id, amount=1, fee)`, which matches the `TokenIdType::Nep245 if amount > 1 => {}` / else-branch `return Pips::ZERO` arm, so every leg contributes `fee_ceil(0-scaled, 1) = 0`, and `fees_collected` for `token_id` ends up `0` instead of `100`. No existing guard (`verify`, nonce checks, `internal_apply_deltas` balance checks) aggregates deltas per `token_id` across intents in the batch for fee purposes — each `TokenDiff::execute_intent` call is fee-independent of any other intent in the same payload.

Note: the question cites `contracts/defuse/core/src/tokens.rs` as the file, but `tokens.rs` contains only transfer/mint event structs; the actual `token_fee`/`execute_intent` logic scoped by the question lives in `contracts/defuse/core/src/intents/token_diff.rs`, which is where this finding is grounded.

### Impact Explanation
Protocol fees intended for `fee_collector` are bypassed entirely for MT (Nep245) and IMT token transfers by any unprivileged signer who structures their own `TokenDiff` batch as many unit legs instead of one aggregate leg. This is repeatable per account, per token, per batch, with no cap other than gas/payload size, matching the explicitly listed Critical category "protocol fees bypassed."

### Likelihood Explanation
The attacker only needs: (1) to hold the Nep245/Imt balance being moved, (2) the ability to construct and sign an arbitrary `DefuseIntents`/`MultiPayload` with many `Intent::TokenDiff` entries (fully within unprivileged capability), and (3) `engine.state.fee() > 0`. No role, relayer key, or DAO action is required. The only cost is the extra payload size/gas for encoding N intents, which is attacker-controlled and can be amortized against the fee saved for high fee rates or large amounts.

### Recommendation
Aggregate the `Nep245`/`Imt` fee-exemption check across all `TokenDiff` intents for the same `token_id` within a single settlement/batch (e.g., sum negative deltas for `token_id` across the whole `DefuseIntents`/execution context before applying the `amount <= 1` exemption), rather than evaluating `token_fee` per individual intent leg. Alternatively, remove or tighten the `amount <= 1` exemption so it cannot be triggered by artificially decomposing a larger transfer into repeated unit-sized `TokenDiff` intents in the same payload.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or a `near-workspaces` sandbox test) that:
1. Sets `engine.state.fee() = Pips::ONE_PERCENT`.
2. Credits the signer with 10,000 units of a `Nep245` `token_id`.
3. Executes intent A: single `MultiPayload` with one `Intent::TokenDiff{ diff: {token_id: -10_000} }`, then asserts `fee_collector` balance for `token_id` equals `Pips::ONE_PERCENT.fee_ceil(10_000) == 100`.
4. Resets state, credits signer with 10,000 units again.
5. Executes intent B: one `MultiPayload` with 10,000 chained `Intent::TokenDiff{ diff: {token_id: -1} }` entries (one signature, `DefuseIntents::intents` vector), then asserts `fee_collector` balance for `token_id` equals `0`.
6. Assert `100 != 0`, proving the fee binding `sum_over_batch(fee_ceil(fee, amount)) == fees_collected.get(token_id)` is violated and strictly lower in the split case, with `fees_collected` computed directly from `TokenDiffEvent::fees_collected` emitted in `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:81-101`).

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

**File:** contracts/defuse/core/src/intents/mod.rs (L30-37)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
}
```

**File:** contracts/defuse/core/src/intents/mod.rs (L97-113)
```rust
impl ExecutableIntent for DefuseIntents {
    fn execute_intent<S, I>(
        self,
        signer_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector,
    {
        for intent in self.intents {
            intent.execute_intent(signer_id, engine, intent_hash)?;
        }
        Ok(())
    }
}
```
