### Title
Unprivileged signer splits an NEP-245 negative delta into per-unit `TokenDiff` intents to bypass protocol fees - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` token deltas when the per-call `amount <= 1`, and `execute_intent` computes the fee independently for each `TokenDiff` intent in the batch rather than on the aggregate delta per token. A signer can therefore submit `M` separate `TokenDiff` intents, each with `delta = -1` on the same `Nep245TokenId`, inside one signed `DefuseIntents`/`MultiPayload`, and pay zero fee instead of `Pips::fee_ceil(protocol_fee, M)`.

### Finding Description
The broken binding is:
`sum_of(fee_collected for token_id across all TokenDiff intents in the batch) == Pips::fee_ceil(protocol_fee, sum_of(|delta| for token_id across the batch))`

In `TokenDiff::execute_intent` [1](#0-0) , the fee for a negative delta is computed per intent call as `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, where `amount = delta.unsigned_abs()` is scoped to that single intent, not to the token's total delta across the batch.

`token_fee` implements the fee-exemption rule [2](#0-1) : for `TokenIdType::Nep245` (and `Imt`), if `amount > 1` the full `fee` is charged, but if `amount <= 1` it returns `Pips::ZERO` — an intentional exemption meant for NFT-like single-unit transfers, but `Nep245TokenId` also represents fungible multi-token balances where "1 unit" has no special meaning.

`DefuseIntents::execute_intent` iterates over `Vec<Intent>` and calls `execute_intent` for each one independently [3](#0-2) , with a single signature/nonce commit covering the whole `Vec<Intent>` [4](#0-3) . There is no aggregation step that sums deltas per `token_id` across the `Vec<Intent>` before computing fees.

Exploit: the attacker signs one `DefusePayload<DefuseIntents>` containing `M` `TokenDiff` intents, each `{ diff: { <same Nep245TokenId>: -1 } }` (each individually balanced elsewhere, e.g. matched by a cooperating counterparty's `+1` deltas in the same batch, or with the attacker's own second account). Each call to `token_fee` sees `amount == 1`, hits the `TokenIdType::Nep245 | TokenIdType::Imt` exemption arm, and returns `Pips::ZERO`, so `fees_collected` for that `Nep245TokenId` stays `0` across all `M` intents — versus `Pips::fee_ceil(protocol_fee, M)` that would have been collected had the same net movement been expressed as one `TokenDiff` with `delta = -M`.

No existing guard prevents this: `MultiPayload::verify`/nonce checks only validate signature and replay-protection, not fee aggregation; `Amounts::add` in `fees_collected` correctly sums whatever `token_fee` returns, but that per-call return value is already zero.

### Impact Explanation
This is a protocol-fee bypass: the `fee_collector` under-collects `Pips::fee_ceil(protocol_fee, M)` of the given `Nep245` token for every batch structured this way, and the attacker's counterparty-matched swap still executes fully (funds move as if fee-free). This directly matches the Critical category "protocol fees bypassed or over-collected." The technique is repeatable across any `Nep245`/`Imt` token, any account, and any batch size `M` (bounded only by gas/message-size limits), so the aggregate shortfall scales linearly with trading volume routed through this pattern.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's control: hold balance of the `Nep245` token being sold, and be able to construct/sign a `MultiPayload` with `M` `TokenDiff` intents instead of one — no special role, relayer key, or contract deployment is required. The only extra requirement is a counterparty (possibly the attacker's own second account, or a normal solver) willing to net the trade to zero across the batch, which is already how NEAR Intents swaps normally work. Cost is simply the extra bytes/gas of `M` intents vs. 1, which is cheap relative to the fee saved on larger `M`. This makes the exploit highly feasible and directly repeatable.

### Recommendation
Aggregate deltas per `token_id` across the whole `DefuseIntents`/batch (or at least across all `TokenDiff` intents from the same signer in the same payload) before applying the `token_fee` exemption threshold, so the "|delta| <= 1" waiver is evaluated against the net signer exposure to that token rather than per individual intent. Alternatively, remove the amount-based exemption for `Nep245`/`Imt` entirely and let `Pips::fee_ceil` naturally produce zero fee only when `protocol_fee * amount` rounds to zero, computed on the aggregated amount.

### Proof of Concept
`cargo test` plan (near-workspaces sandbox, under `tests/`, mirroring existing helpers in `tests/src/tests/defuse/intents/token_diff.rs` and `crates/testing/sandbox/src/extensions/defuse/*`):

1. Deploy defuse contract with `protocol_fee = Pips::ONE_PERCENT` (or similar non-zero fee) and set `fee_collector`.
2. Deposit `M` units (e.g. `M = 100`) of a `Nep245` multi-token to attacker's Verifier balance, and set up a matched counterparty (second account) able to provide the offsetting `+M` delta on another token.
3. **Case A (single intent)**: sign one `MultiPayload` containing one `TokenDiff` intent with `diff = { nep245_token: -100, other_token: +K }`, matched by counterparty's `TokenDiff`. Execute via `execute_intents`. Record `fee_collector` balance of `nep245_token`, expect it to equal `Pips::fee_ceil(protocol_fee, 100)`.
4. **Case B (split intents)**: sign one `MultiPayload` containing 100 `TokenDiff` intents, each `diff = { nep245_token: -1, other_token: +k_i }` (summing to the same net `-100`/`+K`), matched by counterparty's corresponding 100 `+1` diffs. Execute via `execute_intents`.
5. Assert: `fee_collector` balance of `nep245_token` after Case B is `0`, while Case A's `fee_collector` balance is `Pips::fee_ceil(protocol_fee, 100) > 0` — demonstrating `sum_of(fee_collected in Case B) != Pips::fee_ceil(protocol_fee, M)` and the fee-collector shortfall equal to Case A's collected fee.

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
