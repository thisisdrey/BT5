Confirmed: a single `DefuseIntents` payload can contain a `Vec<Intent>`, so an attacker can embed many `TokenDiff` intents in one signed `MultiPayload`, each moving exactly 1 unit of the same `Nep245`/`Imt` `TokenId`, and each one independently bypasses the fee check in `TokenDiff::token_fee`.

### Title
Protocol fee bypass on Nep245/Imt token transfers via unit-splitting of `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` unconditionally returns `Pips::ZERO` for `TokenIdType::Nep245`/`Imt` whenever the per-intent `amount <= 1`, and always for `Nep171`. Because a single signed `MultiPayload` (`DefuseIntents::intents: Vec<Intent>`) can contain an arbitrary number of `TokenDiff` intents, an attacker can split an aggregate Nep245 transfer of `N` units into `N` separate `TokenDiff` intents of `delta = -1` each, so the protocol fee is charged as `0` on every single sub-intent instead of `fee` on the full `N`.

### Finding Description
The binding that should hold is: `balance_of(fee_collector, token_id)_after - balance_of(fee_collector, token_id)_before == protocol_fee.fee_ceil(total_amount_moved_in)` for the aggregate amount of `token_id` moved by the signer within the batch. Instead, per `TokenDiff::execute_intent` [1](#0-0) , fee is computed **per intent** via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, and `token_fee` explicitly zeroes the fee whenever `amount <= 1` for `Nep245`/`Imt` (and always for `Nep171`) [2](#0-1) .

Because `DefuseIntents` allows an arbitrary `Vec<Intent>` under one signature/nonce [3](#0-2) , an attacker holding `N` units of a Nep245 `TokenId` can submit one signed `MultiPayload` with `N` `TokenDiff` intents, each with `diff = {token_id: -1, counter_token: +k}`. Each intent independently evaluates `amount = 1`, so `token_fee` returns `Pips::ZERO` and `fee_ceil(1) == 0`, for every one of the `N` intents. The aggregate signer-to-fee_collector transfer of `N` units of `token_id` (and its counter-value) thus incurs zero fee overall, whereas a single `TokenDiff` with `delta = -N` would have incurred `protocol_fee.fee_ceil(N) > 0` for any `N` large enough. No nonce, signature, or balance-matching guard (`MaybeLegacyNonces::commit`, `TransferMatcher::finalize`, `internal_apply_deltas`) prevents this because each individual intent is valid and balances net correctly per-intent; the fee logic simply treats "amount == 1" as a proxy for "unique NFT-like item" without checking whether the *same* `TokenId` is being moved repeatedly within the same batch/signer.

### Impact Explanation
This bypasses collection of the protocol fee (`fee_collector`'s balance for that `TokenId`) on an arbitrary volume of Nep245/Imt multi-token value, matching the "protocol fees bypassed" Critical category. The attacker/counterparty solver captures the fee differential that should have gone to `fee_collector`. This is repeatable for any signer, any Nep245/Imt `TokenId`, and any counter-token, across any number of batches, scaling with `N` (number of owned units), limited only by the cost of constructing `N` sub-intents in the payload (which is a cost/gas trade-off, not a blocker).

### Likelihood Explanation
Preconditions are modest: the attacker needs to own `N > 1` units of some Nep245/Imt token (or arrange to receive them as part of the same trade) and a willing/matching counterparty for the counter-token side (which can be the attacker's own second account or an automated solver in the matching batch). No privileged role, relayer key, or signature forgery is required — this only requires constructing and signing a normal `MultiPayload` with many well-formed `TokenDiff` intents, which is fully within the attacker's stated capabilities. The economic incentive scales directly with the fee rate and the token value moved, making it attractive whenever `protocol_fee > 0`.

### Recommendation
Compute the Nep245/Imt fee exemption based on properties of the `TokenId` itself (e.g., a per-token flag indicating it represents a unique/NFT-like item, or a per-account/per-token aggregate amount tracked across the whole intent batch) rather than the `amount` of a single `TokenDiff` sub-intent. Alternatively, aggregate all deltas for the same `(signer, token_id)` pair across the entire `DefuseIntents`/batch before applying `token_fee`, so the `amount <= 1` exemption cannot be triggered by splitting a larger transfer into many unit-sized intents.

### Proof of Concept
```rust
// near-workspaces sandbox test (conceptual, in tests/src/tests/defuse/intents/token_diff.rs style)
// 1. Deploy defuse contract with protocol_fee = Pips::ONE_PERCENT (or any nonzero fee).
// 2. Mint/credit attacker with N=1000 units of a Nep245 TokenId `mt:contract:token1`,
//    and credit a counterparty with matching units of `mt:contract:token2`.
// 3. Build ONE signed MultiPayload for attacker containing 1000 TokenDiff intents,
//    each: diff = { token1: -1, token2: +1 } (or whatever ratio matches counterparty side).
//    Pair with counterparty's matching TokenDiff intents (delta token2: -1, token1: +1),
//    each also amount == 1.
// 4. Call execute_intents with both signed payloads batched together.
// 5. Assert:
//    let fee_before = state.balance_of(fee_collector, token1_id);
//    // ... execute ...
//    let fee_after = state.balance_of(fee_collector, token1_id);
//    assert_eq!(fee_after - fee_before, 0); // actual: fee bypassed
//    // Compare against expected fee if done as ONE TokenDiff with delta=-1000:
//    let expected_fee = protocol_fee.fee_ceil(1000);
//    assert!(expected_fee > 0);
//    assert_ne!(fee_after - fee_before, expected_fee); // binding violated
``` [4](#0-3) [2](#0-1) [3](#0-2)

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L41-78)
```rust
impl ExecutableIntent for TokenDiff {
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
        if self.diff.is_empty() {
            return Err(DefuseError::InvalidIntent);
        }

        let protocol_fee = engine.state.fee();
        let mut fees_collected: Amounts = Amounts::default();

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
