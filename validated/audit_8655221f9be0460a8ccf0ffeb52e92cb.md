### Title
Protocol fee on Nep245/Imt (multi-token) `TokenDiff` transfers can be bypassed by splitting a bulk delta into multiple unit (`|delta| == 1`) `TokenDiff` intents - (`crates/near/sender` execution path via `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` decides whether to charge the protocol fee on a Nep245/Imt token move based solely on the magnitude of the delta inside a *single* `TokenDiff` intent invocation (`amount > 1`), never on the aggregate negative delta for that `TokenId` across the whole `execute_intents`/`DefuseIntents` batch. Because `DefuseIntents::execute_intent` simply iterates `self.intents` and calls `TokenDiff::execute_intent` independently for each `Intent::TokenDiff` entry, an attacker can express a large multi-unit MT/IMT transfer as N separate unit-sized (`delta == -1`) `TokenDiff` intents, each individually hitting the `amount > 1` false branch and returning `Pips::ZERO`, causing the protocol to collect zero fee on the whole multi-unit move.

### Finding Description
The broken binding: `fee collected by fee_collector == protocol_fee.fee_ceil(|total negative delta for TokenId X across the execute_intents call|)`.

Code path:
- `TokenDiff::token_fee` (`contracts/defuse/core/src/intents/token_diff.rs:206-216`) exempts `Nep171 | Nep245 | Imt` from fees when the *local* `amount <= 1`: [1](#0-0) 
- `TokenDiff::execute_intent` computes `amount = delta.unsigned_abs()` from the single `TokenDiff.diff` map entry being processed and calls `Self::token_fee(token_id, amount, protocol_fee)` per entry, per call: [2](#0-1) 
- `DefuseIntents::execute_intent` iterates every `Intent` in the batch and executes each `TokenDiff` independently, with no cross-intent aggregation of deltas for fee purposes: [3](#0-2) 
- The only cross-batch check is `TransferMatcher::finalize` in `Deltas`/`engine/state/deltas.rs`, which verifies that total deposits/withdrawals per `TokenId` net to zero (including the fee amounts deposited to the fee collector). It does **not** recompute or enforce a minimum fee based on the aggregate magnitude of the underlying trade — it merely matches whatever deposits/withdrawals (including zero-fee ones) were already recorded: [4](#0-3) 

Since `TokenDeltas`/`Amounts` is a `BTreeMap<TokenId, i128>`, a single `TokenDiff` can only hold one entry per `TokenId` — so an attacker who wants to avoid the `amount > 1` fee test on a specific `Nep245TokenId` simply signs (or bundles) N separate `Intent::TokenDiff` entries, each with `diff: {mt_token_id: -1, ft_token_id: <closure_delta(-1, fee)>}` instead of one `TokenDiff` with `{mt_token_id: -N, ...}`. Each call to `TokenDiff::execute_intent` sees `amount == 1`, so `token_fee` returns `Pips::ZERO` and `fees_collected` stays empty for every one of the N intents; the aggregate fee collected on the whole `execute_intents` call for that `TokenId` is 0, whereas a single `TokenDiff` with `amount == N > 1` would have charged `protocol_fee.fee_ceil(N) > 0` whenever `protocol_fee != 0`.

No existing guard (nonce checks, `verify_intent_nonce`, `TransferMatcher::finalize`, `assert_one_yocto`, pause guards) inspects or aggregates per-token deltas across intents for fee purposes; the invariant check only cares that deposits equal withdrawals, which the attacker satisfies trivially since both sides use the same fee-exempt unit deltas.

### Impact Explanation
This directly matches the "protocol fees bypassed" Critical category defined in the audit rubric. Any unprivileged signer trading Nep245 (multi-token)/Imt assets in quantities greater than 1 can restructure their `TokenDiff` intents as a sequence of unit deltas to avoid all protocol fees that would otherwise apply, at the cost of a slightly larger payload/more intents in the same transaction (no meaningful additional cost, since all intents can be signed once and bundled into one `MultiPayload`/one NEAR transaction). This is repeatable indefinitely, across any account and any Nep245/Imt `TokenId`, and directly reduces protocol revenue captured by `fee_collector` versus the fee that should have applied to the same net economic transfer.

### Likelihood Explanation
Preconditions are minimal and fully within the unprivileged attacker's control: they need to own ≥2 units of some Nep245/Imt `TokenId` (or coordinate with a willing counterparty solver, as in the existing `solver_user_closure` test pattern), and `protocol_fee` must be nonzero (a normal operating configuration). The attack requires no special role, no locked-account bypass, and no cryptographic weakness — only ordinary signed `MultiPayload`s executed via `execute_intents`, which is standard usage. This is highly likely to be exploited by any fee-sensitive high-volume MT/IMT trader.

### Recommendation
Compute the fee-exemption decision for `Nep171`/`Nep245`/`Imt` tokens based on the *aggregate* negative delta for each `TokenId` across the entire batch being executed (i.e., across all `Intent::TokenDiff` entries processed within one `execute_intents`/`DefuseIntents` execution, ideally net of the signer's per-`TokenId` totals), rather than on the magnitude of any single `TokenDiff` intent's delta. This could be implemented by pre-aggregating deltas per signer/`TokenId` before iterating intents, or by tracking cumulative amount already seen for a `TokenId` within the `Deltas`/`TransferMatcher` state and re-evaluating `token_fee` against the running total rather than the local delta.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or an integration test under tests/src/tests/defuse/intents/token_diff.rs)
// using a Nep245TokenId `mt_token_id` and an Nep141 `ft_token_id` as counter-leg.

// 1. Single-intent baseline (amount > 1): fee should be > 0
let fee = Pips::ONE_PERCENT;
let amount: u128 = 4;
let big_fee = TokenDiff::token_fee(&mt_token_id, amount, fee).fee_ceil(amount);
assert!(big_fee > 0);

// 2. Split into 4 unit TokenDiff intents (each amount == 1): fee is 0 each time
let mut total_fee_split = 0u128;
for _ in 0..4 {
    let f = TokenDiff::token_fee(&mt_token_id, 1, fee).fee_ceil(1);
    total_fee_split += f;
}
assert_eq!(total_fee_split, 0);

// 3. Assert violation of the claimed binding:
// fee_collected(aggregate split) == protocol_fee.fee_ceil(total negative delta) should hold but does not:
assert_ne!(total_fee_split, big_fee); // 0 != big_fee, demonstrating the bypass

// End-to-end (near-workspaces sandbox) proof:
// - Bundle N=4 Intent::TokenDiff entries, each {mt_token_id: -1, ft_token_id: closure_delta(-1, fee)},
//   signed by the attacker, plus matching counterparty TokenDiff intents supplying +1 mt_token_id / -closure ft_token_id each.
// - Call execute_intents with this MultiPayload batch.
// - Assert fee_collector's mt_token_id balance increase == 0.
// - Compare against a single TokenDiff {mt_token_id: -4, ...} equivalent trade signed instead,
//   and assert fee_collector's balance increase == protocol_fee.fee_ceil(4) > 0.
```

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

**File:** contracts/defuse/core/src/intents/mod.rs (L97-112)
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
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-283)
```rust
    // Finalizes all transfers, or returns unmatched deltas.
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
