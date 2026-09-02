### Title
Protocol fee bypass on NEP-245/IMT `TokenDiff` intents via splitting a multi-unit delta into unit-sized (`|delta|<=1`) intents - (`contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::execute_intent` computes and collects the protocol fee independently for each `TokenDiff` intent, based solely on that intent's own per-token `delta`. Because `TokenDiff::token_fee` waives the fee entirely for `Nep245`/`Imt` tokens whenever `|delta| <= 1`, a signer can split one negative delta of `-2` (or any even/any multiple decomposition into unit steps) into multiple `-1` intents on the same token and pay zero fee instead of `Pips::fee_ceil(fee, 2)`.

### Finding Description
The broken binding: for a fixed protocol `fee: Pips` and a fixed negative delta magnitude `amount` on a `Nep245`/`Imt` `TokenId` `T`, the fee actually credited to `fee_collector` should be invariant to how the signer chooses to partition that `amount` across `TokenDiff` intents in the batch:
```
fees_collected(T) == fee.fee_ceil(amount)   for any decomposition amount = sum(amount_i)
```

In code, `TokenDiff::execute_intent` computes the fee per-intent, per-token-entry: [1](#0-0) 

and `TokenDiff::token_fee` waives fees entirely for `Nep245`/`Imt` when the *single intent's* `amount <= 1`: [2](#0-1) 

`DefuseIntents::execute_intent` simply loops over all intents in the signed message, calling `execute_intent` on each independently, with no cross-intent aggregation of deltas before fee computation: [3](#0-2) 

Exploit: the signer constructs a single `MultiPayload` whose `DefuseIntents.intents` contains two `TokenDiff` intents, each `diff: {T: -1}` (same NEP-245 or IMT `TokenId` `T`), instead of one `TokenDiff` with `diff: {T: -2}`. For the unsplit intent, `amount = 2 > 1`, so `token_fee` returns the full `fee`, and `fees_collected[T] = fee.fee_ceil(2)`. For the split intents, each has `amount = 1`, so `token_fee` returns `Pips::ZERO` both times, and total `fees_collected[T] = 0`. The counterparty side of the trade (the matching positive deltas that satisfy `TransferMatcher::finalize`) is unaffected by this split, so the net economic trade executed is identical in both cases; only the fee differs.

None of the existing guards intervene: `MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, `SaltRegistry::is_valid`, and `TransferMatcher::finalize` all validate signature/nonce/balance-matching correctness, but none of them aggregate or re-derive fees across multiple `TokenDiff` intents signed in the same batch, and `assert_one_yocto`/access-control guards are not relevant to intent execution.

### Impact Explanation
This under-collects protocol fees credited to `fee_collector` for any NEP-245 (`Nep245TokenId`) or IMT (`ImtTokenId`) token whenever a trade's `token_in` amount can be decomposed into a sequence of unit (`amount<=1`) steps — the fee for that token drops to zero regardless of protocol `fee` configuration. This is repeatable by any unprivileged signer, for any NEP-245/IMT token, in every batch, with no cap — the entire `amount` of a token can be moved in `amount` unit-sized `TokenDiff` intents (bounded only by intent count/gas), fully avoiding the intended fee. This matches "protocol fees bypassed" in the Critical impact category.

### Likelihood Explanation
No special privileges are required: any account holding NEP-245/IMT balances in the Verifier can sign a `MultiPayload` with multiple `TokenDiff` intents of `|delta|=1` on the same token instead of one intent with a larger `|delta|`, as long as a matching counterparty (or their own additional signed intents) supplies the offsetting positive deltas so `TransferMatcher::finalize` nets to zero. The attacker's cost is only the extra intents/gas needed to split the trade into unit steps; the fee savings are deterministic and scale with the trade size.

### Recommendation
Aggregate per-token, per-signer negative deltas across all `TokenDiff` intents in the batch (or across the whole `execute_signed_intents` call) before applying the NEP-245/IMT `amount <= 1` fee-waiver threshold, so that fee computation depends on the total amount moved for a token rather than the size of an individual intent's delta entry.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or `tests/src/tests/defuse/intents/token_diff.rs` with a sandbox `MultiPayload`):
1. Build a `TokenDiff` with `diff = {T: -2}` for a `Nep245TokenId` `T`, with `fee = Pips::ONE_PERCENT` (or similar nonzero); call `TokenDiff::execute_intent` (or run through `Engine::execute_signed_intents`) and record `fees_collected[T]`. Assert it equals `fee.fee_ceil(2)` (nonzero).
2. Build two `TokenDiff` intents each with `diff = {T: -1}` signed by the same account in one `MultiPayload` (with matching offsetting positive deltas elsewhere in the batch to satisfy `TransferMatcher::finalize`); sum `fees_collected[T]` across both `TokenDiffEvent`s. Assert it equals `0`.
3. Assert the two totals from steps 1 and 2 differ (`fee.fee_ceil(2) != 0`), demonstrating the fee depends on how the identical economic trade is partitioned into intents, violating the fee invariant.

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
