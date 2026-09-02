### Title
Protocol fee bypass on NEP-245/IMT tokens via TokenDiff splitting into unit-amount legs - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives the protocol fee whenever the per-intent `amount <= 1` for `TokenIdType::Nep245`/`Imt` tokens, and this threshold is evaluated independently for every `TokenDiff` intent in a batch with no aggregation across intents or across the whole `MultiPayload`. An unprivileged signer can therefore split one legitimate `-1_000_000` NEP-245 delta into 1,000,000 separately signed `-1` delta intents, each landing in the zero-fee branch, so the fee_collector receives `0` instead of `Pips::fee_ceil(1_000_000, protocol_fee)`.

### Finding Description
The broken binding is: `sum(fees_collected[T] over the batch) == Pips::fee_ceil(sum(|negative deltas of T|), protocol_fee)`.

`TokenDiff::execute_intent` computes fees per intent, iterating only over that single intent's own `diff: BTreeMap<TokenId, i128>`: [1](#0-0) 

The fee amount used is `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, where `token_fee` is: [2](#0-1) 

For `TokenIdType::Nep245` (and `Imt`), the function returns `Pips::ZERO` whenever `amount <= 1`, and only applies the real `fee` when `amount > 1`. This rule is per-intent: each `TokenDiff` intent's `diff` map can contain a given `TokenId` only once (map semantics prevent duplicate keys within one intent), so there is no way to combine two deltas on the same token inside a single intent to trigger the `amount > 1` branch. Nothing in `execute_intents`/the batch-processing loop aggregates deltas on the same `TokenId` across multiple `TokenDiff` intents before calling `token_fee`; each intent is executed and fee-collected independently at lines 70-78.

Exploit: the attacker holds `1_000_000` units of `Nep245TokenId{contract_id, mt_token_id}` inside the Verifier. Instead of signing one `TokenDiff{diff: {token_id: -1_000_000, ...}}` (which would trigger the real `fee` since `amount=1_000_000>1`), they sign `1_000_000` separate `TokenDiff` payloads, each `{diff: {token_id: -1, ...}}`, each with a distinct nonce, and submit them together (or across multiple `execute_intents` calls) as their own balance-decreasing legs paired with matching balance-increasing legs elsewhere. Every one of the million legs hits the `amount <= 1` branch and returns `Pips::ZERO`, so `fees_collected` for that token stays `Amounts::default()` for each leg, and the aggregate fee credited to `fee_collector` is `0` instead of `fee_ceil(1_000_000, protocol_fee)`.

No existing guard catches this: `MultiPayload::verify` / nonce checks only ensure signature and replay-protection validity per intent, not fee aggregation; `TransferMatcher::finalize` matches balance deltas for invariant purposes but does not re-derive or aggregate fees; there is no batch-wide or historical aggregation of `token_fee` inputs per token per signer.

### Impact Explanation
This under-collects protocol fees paid to `fee_collector` — value that the protocol is entitled to per its own fee model never leaves the payer's/counterparty's flow to the collector. This matches the in-scope Critical category "protocol fees bypassed or over-collected." The blast radius is any account holding NEP-245/IMT balances in the Verifier and any protocol_fee > 0 configuration; it's repeatable per token, per account, and per batch — the only cost to the attacker is producing/signing more payloads and paying the incremental NEAR gas for a larger batch (or several sequential `execute_intents` calls), not a meaningful economic constraint relative to the fee saved on a fungible-like NEP-245 balance.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged signer's capability: any account balance in a NEP-245 token inside the Verifier, `protocol_fee > 0`, and the ability to sign multiple `DefusePayload`s with distinct nonces (a normal, permitted operation). No role, relayer key, or DAO action is required. Splitting into unit legs is directly supported by the existing per-intent `token_fee` code path; the discrepancy is deterministic and can be shown with as few as two `-1` legs vs one `-2` leg, so the "1,000,000" figure is illustrative of scale, not a required minimum to prove the bug.

### Recommendation
Aggregate the amount used for the NEP-245/Imt zero-fee heuristic per `TokenId` across the whole batch (or per signer/session) rather than per individual `TokenDiff` intent, e.g., pre-sum all negative deltas per `TokenId` across all `TokenDiff` intents in the `MultiPayload` before evaluating `amount <= 1` in `TokenDiff::token_fee`, or remove/restrict this exemption to true NFTs (`Nep171`) where `amount` is inherently bounded to 1, since NEP-245/IMT tokens can be fungible/semi-fungible with large balances.

### Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (unit test area)
#[test]
fn nep245_fee_bypass_via_splitting() {
    let token_id = Nep245TokenId::new(
        "mt.near".parse().unwrap(),
        "ft1".to_string(),
    ).into();
    let fee = Pips::ONE_PERCENT * 5; // protocol_fee > 0

    // Aggregate leg: -1_000_000 in a single intent
    let aggregate_fee = TokenDiff::token_fee(&token_id, 1_000_000, fee).fee_ceil(1_000_000);
    assert!(aggregate_fee > 0);

    // Split into 1_000_000 unit legs
    let mut total_split_fee: u128 = 0;
    for _ in 0..1_000_000u64 {
        let leg_fee = TokenDiff::token_fee(&token_id, 1, fee).fee_ceil(1);
        total_split_fee += leg_fee;
    }

    // Binding violated: split fees (0) != aggregate fee (>0)
    assert_ne!(total_split_fee, aggregate_fee);
    assert_eq!(total_split_fee, 0);
}
```
For an end-to-end sandbox proof (`tests/src/tests/defuse/intents/token_diff.rs`), construct two `execute_intents` batches with equivalent net balance changes for the same signer/token — one batch with a single `TokenDiff{diff:{token_id: -N}}` intent, another with `N` separately signed `TokenDiff{diff:{token_id: -1}}` intents (each nonce-distinct) — and assert `fees_collected` events / `fee_collector`'s post-balance for the token differ between the two runs despite identical net token movement.

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
