### Title
Fee bypass on `Nep245`/`Imt` `TokenDiff` intents via splitting into unit-amount pieces - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` token diffs whenever the per-intent `amount <= 1`, and `TokenDiff::execute_intent` computes the fee independently for each `TokenDiff` intent using only that intent's own delta magnitude, with no aggregation across intents in the same `MultiPayload` batch. An attacker who is a party to a large `Nep245`/`Imt` trade can sign N separate `TokenDiff` intents of amount 1 instead of one intent of amount N, causing `token_fee` to return `Pips::ZERO` on every sub-intent while `TransferMatcher::finalize` still nets and settles the full aggregate amount.

### Finding Description
The broken binding is: `FEE_COLLECTED == fee_ceil(sum(|negative_delta| for all TokenDiff intents on token X from signer S in this batch), protocol_fee)` should hold for the effective economic trade size, but the code computes it per-intent instead: [1](#0-0) 
using `Self::token_fee(token_id, amount, protocol_fee)` where `amount` is `delta.unsigned_abs()` of a single intent's diff entry, and: [2](#0-1) 
which returns `Pips::ZERO` whenever the token type is `Nep245`/`Imt` and `amount <= 1`.

Since each `TokenDiff` is processed independently in `execute_intent`, and `engine.state.internal_apply_deltas`/`internal_sub_balance` feed a batch-wide `TransferMatcher` (`contracts/defuse/core/src/engine/state/deltas.rs`, `TransferMatcher::finalize`) that only cares that deltas across the whole batch net to zero — not which intent or how many intents contributed them — an attacker who is the party paying out the `Nep245`/`Imt` asset can split a single 100-unit token-in delta into 100 separate self-signed `TokenDiff` intents, each with delta `-1` on that token (each with a distinct `Nonce`), submitted in the same `MultiPayload` batch alongside the counterparty's single `+100`/`-100` matching intent. Each of the 100 sub-intents independently satisfies `token_fee(Nep245, 1, fee) == Pips::ZERO`, so `fee_ceil(1) == 0` for all of them, while `TransferMatcher::finalize` still correctly aggregates and settles the full 100-unit transfer between the two parties. Existing guards (`MultiPayload::verify`, nonce uniqueness, `TransferMatcher::finalize`'s zero-sum invariant) only ensure balance integrity and signature validity — they do not re-aggregate deltas per signer/token before fee computation, so none of them prevent this split.

### Impact Explanation
The protocol permanently under-collects fees on the `Nep245`/`Imt` side of any trade the attacker structures this way: the `fee_collector` account receives strictly less than `fee_ceil(total_amount, protocol_fee)`, with the shortfall retained by the attacker. This matches the Critical category "protocol fees bypassed or over-collected." It is fully repeatable by any account, for any `Nep245`/`Imt` token, and any batch size, at the cost only of constructing/signing more intents (more gas, but gas cost is explicitly out of scope as a blocking factor and does not offset the fee saved for larger trades).

### Likelihood Explanation
No privileged role, victim key, or special contract state is required — the attacker only needs to be a legitimate party to their own trade and is free to choose how many `TokenDiff` intents to sign for their own delta. This is trivially reproducible off-chain by any user submitting a `MultiPayload` with `execute_intents`, using their own account, their own tokens, and their own nonces.

### Recommendation
Aggregate negative deltas per `(signer_id, token_id)` across the whole batch (or across the whole `TokenDiff` set being executed) before applying `token_fee`'s amount-based exemption, rather than evaluating the exemption per individual `TokenDiff` intent.

### Proof of Concept
`cargo test` (in `contracts/defuse/core` or via `near-workspaces` sandbox, non-mainnet) plan:
1. Set up a `Nep245` token and two accounts A (seller) and B (buyer), with `protocol_fee = Pips::ONE_PERCENT` (non-zero).
2. Baseline: submit a single `TokenDiff` from A `{Nep245: -100, Nep141Usdc: +100}` and matching from B `{Nep245: +100, Nep141Usdc: -100}` in one `execute_intents` batch; assert `fee_collector` balance for `Nep245` equals `fee_ceil(100, protocol_fee) > 0` (assert both sides of the binding: `fee_owed == fee_collected` and both are nonzero).
3. Exploit: submit 100 `TokenDiff` intents from A, each `{Nep245: -1, Nep141Usdc: +1}` (distinct nonces), plus B's single `{Nep245: +100, Nep141Usdc: -100}`, in one `execute_intents` batch.
4. Assert the `TransferMatcher`/`Transfers` settle the full 100-unit `Nep245` transfer from A to B (balances update correctly, i.e. balance-neutrality holds).
5. Assert `fee_collector`'s `Nep245` balance stays `0` after step 3, demonstrating `fee_owed (fee_ceil(100, fee) > 0) != fee_collected (0)`.

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
