### Title
Protocol fees on Nep245/Imt tokens bypassed by splitting a large TokenDiff into many unit-amount legs - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Finding Description
`TokenDiff::token_fee` explicitly waives the protocol fee for `Nep245`/`Imt` token diffs whenever `amount <= 1`: [1](#0-0) . This is applied per-intent in `execute_intent`, where `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` is computed independently for each `TokenDiff` intent processed in the batch: [2](#0-1) .

The intended binding (as encoded by `supply_delta`/`closure_supply_delta`, which model the fee owed for a *single* `TokenDiff` on a given token with a given aggregate `delta`) is: `total_fee_collected == Pips::fee_ceil(protocol_fee, |B|)` for a net transferred volume `B` on a fee-liable token. Because fee computation is scoped to each individual intent's `amount` rather than to the aggregate volume moved across the whole `MultiPayload`/batch, an attacker can decompose one `TokenDiff{token: -B}` into `B` separate `TokenDiff{token: -1}` intents (with `B` matching `+1` counter-legs signed by a cooperating/attacker-controlled counterparty account). Each individual leg has `amount = 1`, so `token_fee` returns `Pips::ZERO`, and `fee_ceil(1) == 0` for every leg — regardless of `protocol_fee`. The whole batch still satisfies the `TransferMatcher::finalize` conservation check (`contracts/defuse/core/src/engine/state/deltas.rs`, lines 267-283) since withdrawals and deposits net to zero per token without requiring any extra fee-collector credit, so no `InvariantViolated` error is raised, no signature/nonce/lock check blocks it (each leg is a normally signed, validly nonced intent), and the batch executes successfully with `total_fee_collected = 0` for a true moved volume `B`.

### Impact Explanation
This under-collects protocol fees on `Nep245`/`Imt` token diffs: the `fee_collector` account receives strictly less (zero, if the entire volume is chunked into 1-unit legs) than `Pips::fee_ceil(protocol_fee, B)` that would be owed for a single `TokenDiff{-B}`. This matches the listed Critical impact category "protocol fees bypassed or over-collected." It is repeatable for any account pair holding/controlling a `Nep245`/`Imt` balance and any batch size, and scales with the fee rate and volume moved; it does not itself move funds out of the Verifier beyond what was already owed, but it deprives the fee collector of due revenue.

### Likelihood Explanation
Preconditions: attacker (optionally using two self-controlled accounts to avoid needing an unrelated counterparty) holds/receives a `Nep245`/`Imt` balance of volume `B`; a positive protocol fee (`Pips`) is configured. The attacker signs `B` `TokenDiff{token: -1}` intents and pairs them with `B` matching `+1` legs (self or willing counterparty), submitted in one or several `execute_intents` calls. Cost scales with the number of legs (extra signatures/gas), and grows linearly with `B`, so this is most attractive for high-value tokens/large volumes where the fee saved by evasion exceeds the extra signing/gas overhead. No privileged role or DAO permission is required; only ability to sign and submit `MultiPayload`s that any user already has.

### Recommendation
Compute and charge the fee for `Nep245`/`Imt` (and, if applicable, `Nep171`) `TokenDiff` legs based on the aggregated per-token, per-signer (or per-batch) delta rather than per individual intent leg — e.g., accumulate `Nep245`/`Imt` deltas across the whole batch before evaluating `token_fee`'s `amount > 1` threshold, or remove the `amount <= 1` fee exemption and instead handle the legitimate NFT/atomic single-unit rounding concern with a minimum-fee floor exemption tied to true NFT semantics (`Nep171`) only, not to fungible-style `Nep245`/`Imt` balances that can be freely split.

### Proof of Concept
`cargo test` (in `contracts/defuse/core` or the sandbox `tests/` crate covering `TokenDiff`) comparing:
1. Executing one `TokenDiff{ diff: {mt_token: -B} }` (signer) matched by one counterparty `TokenDiff{ diff: {mt_token: +closure_amount} }` computed via `TokenDiff::closure_delta(&mt_token, -B, fee)` with `fee = Pips::ONE_PERCENT` and `B = 1000` on a `Nep245`/`Imt` token — assert `fees_collected` for `mt_token` equals `fee.fee_ceil(1000)` (nonzero).
2. Executing `B = 1000` separate `TokenDiff{ diff: {mt_token: -1} }` intents matched by `1000` separate `TokenDiff{ diff: {mt_token: +1} }` counter-legs in the same batch — assert the sum of `fees_collected` across all `TokenDiffEvent`s for `mt_token` is `0`, and that the resulting `fee_collector` balance for `mt_token` is unchanged, while the net transferred volume (`B`) between the two accounts is identical to case 1.
Assert the two `total_fee_collected` values differ (`0 != fee.fee_ceil(1000)`), proving the fee-collection binding is broken by leg-splitting.

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
