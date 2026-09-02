### Title
Splitting TokenDiff into unit-size legs bypasses per-transaction fee on Nep245/Imt tokens - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::execute_intent` computes and collects the protocol fee independently for each `TokenDiff` intent, using only that intent's own `|delta|` as the `amount` argument to `TokenDiff::token_fee`. Because `token_fee` waives fees entirely for `Nep245`/`Imt` (and `Nep171`) token types whenever `amount <= 1`, an attacker who splits one large negative delta into many unit-size (`delta = -1`) `TokenDiff` intents pays zero fee on each leg, whereas a single intent with the full delta would have paid `fee.fee_ceil(total_amount)`.

### Finding Description
The binding that should hold is:
`sum_i Pips::fee_ceil(fee, |delta_i|) == Pips::fee_ceil(fee, |sum_i delta_i|)` for all negative deltas on the same token `T` executed within a batch.

In `TokenDiff::execute_intent` [1](#0-0) , for every negative delta the fee is computed as `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` where `amount` is `delta.unsigned_abs()` for that single intent only — there is no aggregation across intents in the batch.

`TokenDiff::token_fee` [2](#0-1)  returns `Pips::ZERO` for `Nep171`/`Nep245`/`Imt` token types whenever `amount <= 1`, and only charges the real `fee` for `Nep245`/`Imt` when `amount > 1` (Nep141 is always charged regardless of amount).

`Pips::fee_ceil` is a straightforward `checked_mul_div_ceil` of amount by the fee ratio [3](#0-2) , so `fee_ceil(0-fee, 1) == 0` for any fee ratio while `fee_ceil(fee, 1000) > 0` for `fee > 0`.

Exploit path: the attacker controls two of their own accounts, A (sender) and B (receiver) of a Nep245/Imt token. Instead of one `TokenDiff` intent with `{T: -1000}` (signed by A) and `{T: +1000}` (signed by B) in a single `MultiPayload`, the attacker submits 1000 pairs of intents, each pair being `{T: -1}` (A) / `{T: +1}` (B), all inside one `MultiPayload` batch executed via `execute_intents`. The `TransferMatcher::finalize` mechanism (`contracts/defuse/core/src/engine/state/deltas.rs`) only requires that all deltas across the whole batch net to zero per token — confirmed by the `invariant_violated` test showing unmatched deltas cause a rejected batch [4](#0-3)  — it does not require the legs to be a single intent, nor does it re-derive or aggregate the fee. Each of the 1000 `TokenDiff::execute_intent` calls independently computes `token_fee(T, 1, fee)`, which returns `Pips::ZERO` because `amount == 1`, so `fees_collected` for that leg is `0`. Summed over the whole batch, `fees_collected == 0`, while a single unsplit `-1000`/`+1000` pair would have charged `fee.fee_ceil(1000) > 0`.

No existing guard prevents this: `MultiPayload::verify`, nonce/signature checks, and `TransferMatcher::finalize`'s zero-sum invariant all operate on balance conservation, not on fee correctness, and none of them re-price the transfer based on the aggregate size of the underlying trade.

### Impact Explanation
The fee collector (`engine.state.fee_collector()`) under-collects protocol fees on Nep245 (multi-token, e.g., wrapped/bridged assets) and Imt token types. Any attacker moving value between two accounts they control (no victim required) can transfer an arbitrary total amount of a fee-bearing Nep245/Imt asset for zero fee by splitting it into unit legs, at the cost of extra transactions/intents. This matches the "protocol fees bypassed" Critical impact category from the rules. It is fully repeatable across accounts, tokens, and batches, and does not require any privileged role, victim signature, or arithmetic overflow.

### Likelihood Explanation
The attacker only needs: (1) ownership of a Nep245/Imt balance (theirs, no victim funds needed), (2) the ability to sign and submit `MultiPayload` intents (baseline capability of any user), and (3) willingness to pay gas for N intents instead of 1. There are no rate limits or amount checks preventing arbitrarily many unit-delta `TokenDiff` intents in one batch. This is highly feasible and cheap relative to the fee saved on any sizeable transfer, and it's trivially repeatable.

### Recommendation
Compute and charge the Nep245/Imt/Nep171 amount-gating fee based on the net negative delta per token across the entire executed batch (or per signer session) rather than per individual `TokenDiff` intent — e.g., aggregate `|delta|` per `(signer, token_id)` before evaluating `TokenDiff::token_fee`, or remove/tighten the `amount <= 1` fee waiver so it cannot be trivially defeated by unit-sized splitting (e.g., base the waiver on token semantics — NFTs are inherently amount=1 — rather than a raw threshold applicable to fungible-like Nep245/Imt balances).

### Proof of Concept
```rust
// cargo test -p defuse-core (or equivalent integration test crate)
// using Nep245TokenId as in token_diff.rs's own test module.

use defuse_core::{
    fees::Pips,
    intents::token_diff::{TokenDiff, TokenDeltas},
    token_id::nep245::Nep245TokenId,
};

#[test]
fn split_bypasses_fee_on_nep245() {
    let fee = Pips::ONE_PERCENT; // nonzero fee
    let token_id = Nep245TokenId::new("mt.near".parse().unwrap(), "ft1".to_string()).into();

    // Single intent: delta = -1000
    let single_fee = TokenDiff::token_fee(&token_id, 1000, fee).fee_ceil(1000);
    assert!(single_fee > 0, "expected nonzero fee for amount=1000");

    // 1000 split intents: delta = -1 each
    let mut split_fee_sum: u128 = 0;
    for _ in 0..1000 {
        split_fee_sum += TokenDiff::token_fee(&token_id, 1, fee).fee_ceil(1);
    }

    // Binding violated: sum of split fees != fee on combined amount
    assert_eq!(split_fee_sum, 0);
    assert_ne!(split_fee_sum, single_fee);
}
```

For an end-to-end sandbox proof, extend `tests/src/tests/defuse/intents/token_diff.rs`'s `swap_many`/`test_ft_diffs` harness with an Nep245 token: execute a `MultiPayload` with 1000 `TokenDiff` intent pairs (`{T:-1}` signed by account A / `{T:+1}` signed by account B) and compare the summed `fees_collected` in emitted `TokenDiffEvent`s against a single `{T:-1000}`/`{T:+1000}` pair's `fees_collected`, asserting the former is `0` while the latter is `fee.fee_ceil(1000)`.

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

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L303-345)
```rust
    let signed = try_join_all([
        user1.sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), -1000),
                        (ft2_token_id.clone(), 2000),
                    ])
                    .unwrap(),
                memo: None,
                referral: None,
            }],
        ),
        user1.sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), 1000),
                        (ft2_token_id.clone(), -1999),
                    ])
                    .unwrap(),
                memo: None,
                referral: None,
            }],
        ),
    ])
    .await
    .unwrap();

    assert_eq!(
        env.defuse
            .simulate_intents(MultiPayloadArgs { signed: &signed })
            .await
            .unwrap()
            .invariant_violated
            .unwrap()
            .into_unmatched_deltas(),
        Some(TokenDeltas::new(
            std::iter::once((ft2_token_id.clone(), 1)).collect()
        ))
    );
```
