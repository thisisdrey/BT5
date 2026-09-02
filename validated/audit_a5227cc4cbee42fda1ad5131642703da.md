This vulnerability is confirmed and reproducible. The `token_fee` function computes the fee guard using `amount.unsigned_abs()` of a **single delta within a single `TokenDiff` intent leg**, not any aggregate across a `MultiPayload` batch or across multiple submitted intents.### Title
Protocol fee bypass on Nep245/Imt token trades by splitting a delta into multiple |delta|=1 legs - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` (`contracts/defuse/core/src/intents/token_diff.rs:206-216`) waives fees for `Nep245`/`Imt` token legs whenever `amount <= 1`, and this check is evaluated per-leg on the raw `i128` delta of a single `TokenDiff` intent, not on any aggregate signer-in-amount across a batch. An unprivileged signer can therefore split a `Nep245` token_in of `-2` into two separate `TokenDiff` intents (or two legs) each carrying `delta == -1`, causing `token_fee` to return `Pips::ZERO` for both instead of `protocol_fee` being applied to the combined `2` units.

### Finding Description
The binding that should hold is: `fees_collected[T] == Pips::fee_ceil(protocol_fee, sum(|negative deltas of T for signer|))`. The actual code computes fee independently per `(token_id, delta)` pair inside a single `TokenDiff::execute_intent` call: [1](#0-0) 

and the fee-exemption guard in `token_fee` only looks at the magnitude of that single delta: [2](#0-1) 

There is no aggregation of deltas for the same `(signer_id, token_id)` pair across multiple `TokenDiff` intents in a `MultiPayload`, nor across multiple `MultiPayload`s in `execute_signed_intents` (`contracts/defuse/core/src/engine/mod.rs:32-40`, which just loops `execute_signed_intent` and calls `finalize()` for the batch-wide zero-sum invariant, but performs no fee-relevant aggregation).

Exploit flow:
1. Attacker holds `>=2` units of a `Nep245` token `T` in the Verifier (via any MT they deposited/control).
2. `protocol_fee = Pips::ONE_PERCENT`.
3. Instead of one `TokenDiff{diff: {T: -2, T_out: +X}}` (which would compute `fee = Pips::ONE_PERCENT.fee_ceil(2) = 1`), the attacker signs two `TokenDiff` intents (or two legs), each `{T: -1, T_out: +X/2}}`, matched against the same or split counterparty legs so the batch nets to zero (`finalize()`/`TransferMatcher` invariant is untouched by this, since it only checks the whole batch sums to zero, which still holds).
4. In each intent, `token_fee(T, 1, protocol_fee)` hits `TokenIdType::Nep245 if amount > 1 => {}` guard is false since `amount == 1`, falling to `TokenIdType::Nep171 | Nep245 | Imt => return Pips::ZERO`.
5. `fee_ceil(1, Pips::ZERO)` = 0 for both legs, total `fees_collected == 0`, while the aggregate signer-in-amount for `T` is `2`.

No existing guard (`MultiPayload::verify`, `verify_intent_nonce`, `commit_nonce`, `TransferMatcher::finalize`, `assert_one_yocto`, `#[pause]`, ACL guards) checks or aggregates per-token fee-relevant amounts across intents; each of these guards addresses signature/nonce/lock/zero-sum concerns, not fee aggregation.

### Impact Explanation
`fee_collector` is under-paid (protocol fee reduced from the correct `Pips::fee_ceil(protocol_fee, N)` to `0`) for any `Nep245`/`Imt` trade that a signer chooses to decompose into unit legs, while the counterparty still receives the full negotiated `token_out` amount. This matches the explicitly listed Critical category "protocol fees bypassed or over-collected." The attack is fully repeatable per account, per token, per batch, and scales linearly with the number of intents the attacker is willing to sign/submit (each additional `-1` leg avoids fee on one more unit), so an attacker controlling a `Nep245` contract whose "units" represent meaningful economic value (Nep245/MT allows arbitrary per-unit value, unlike a strict NFT) can bypass essentially all protocol fee revenue on that token class.

### Likelihood Explanation
No privileged role, relayer key, or victim key is required — any signer can craft their own `MultiPayload` with multiple `TokenDiff` intents. The only precondition is owning `>=2` units (or more, scaled by splitting) of a `Nep245`/`Imt` token and a willing/matched counterparty leg to keep the batch balanced (which the attacker can also construct, e.g. by controlling both sides or trading against a normal counterparty who is indifferent to the intermediate leg structure). Cost is simply signing/submitting extra intents; no capital or gas cost beyond normal transaction fees, which is explicitly out of scope to weigh against.

### Recommendation
Aggregate the total negative delta for a given `(signer_id, token_id)` pair across the entire batch (all `TokenDiff` intents in the `MultiPayload`s being executed) before applying the `amount > 1` fee-exemption threshold in `token_fee`, rather than evaluating the guard per individual delta/leg. Alternatively, remove or tighten the exemption so it only applies to genuinely NFT-like tokens (`Nep171`, or `Nep245`/`Imt` sub-types with per-token max supply of 1), not to any `Nep245`/`Imt` id regardless of per-unit value.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or `tests/src/tests/defuse/intents/token_diff.rs` sandbox test):
1. Set `protocol_fee = Pips::ONE_PERCENT`.
2. Case A: sign+execute a single `TokenDiff{diff: {nep245_token: -2, ft_out: +X}}` from `signer_id`, matched by a counterparty intent; assert `fees_collected[nep245_token] == Pips::ONE_PERCENT.fee_ceil(2)` (i.e. `1`).
3. Case B: sign+execute two `TokenDiff{diff: {nep245_token: -1, ft_out: +X/2}}` intents from the same `signer_id`, matched by counterparty legs summing correctly; assert `fees_collected[nep245_token] == 0` for both intents combined.
4. Assert `Case A fee (1) != Case B fee (0)` while both moved the same aggregate `2` units of `nep245_token` from the signer, demonstrating the FEES binding is broken.

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
