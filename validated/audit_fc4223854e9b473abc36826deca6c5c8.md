Confirmed: a single `DefuseIntents` (one signed `MultiPayload`, one nonce) can contain a `Vec<Intent>` where multiple `Intent::TokenDiff(...)` entries appear back-to-back, each executed independently via `Intent::execute_intent` in the loop at `contracts/defuse/core/src/intents/mod.rs` (lines 108-113). Each `TokenDiff::execute_intent` computes its fee purely from its own `diff` map's per-token delta magnitude, calling `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` at `contracts/defuse/core/src/intents/token_diff.rs` lines 70-78, with no cross-intent aggregation of the same `TokenId`'s deltas within the batch. `token_fee` (lines 206-217) returns `Pips::ZERO` for `TokenIdType::Nep245 | TokenIdType::Imt` whenever `amount <= 1`. [1](#0-0) [2](#0-1) [3](#0-2) 

So an attacker signing one `DefuseIntents` payload with `intents: [TokenDiff{-1}, TokenDiff{-1}, ..., TokenDiff{-1}]` (N times) for the same Imt/Nep245 `TokenId` pays `0` fee total, whereas one `TokenDiff{-N}` would pay `Pips::fee_ceil(protocol_fee, N)`. The `TransferMatcher`/`Transfers::finalize` machinery in `contracts/defuse/core/src/engine/state/deltas.rs` only nets balances across the whole batch to enforce the zero-sum invariant — it does **not** recompute or re-derive fees; fees are baked in per-`TokenDiff` execution before that netting happens (fee is added into `fees_collected` and credited via `internal_add_balance` at lines 96-101 of `token_diff.rs`, decoupled from `TransferMatcher::finalize`). [4](#0-3) [5](#0-4) 

No nonce, signature, `has_public_key`, or `TransferMatcher` invariant check gates this — it's purely a per-intent fee-calculation gap. The only requirement is that the whole batch still nets to zero across all TokenDiff deltas (enforced by `TransferMatcher::finalize`'s `InvariantViolated` check), which a solver/counterparty can trivially satisfy by matching each `-1` leg with a corresponding `+1` leg elsewhere in the batch (their own or a colluding signer's intents) — this doesn't require any privileged role, just an unprivileged signer plus a counterparty willing to also split their side into unit legs (or the same signer swapping tokens with themself/an alt account they control).

### Title
Fee bypass on Nep245/Imt `TokenDiff` transfers via unit-delta splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives fees for `TokenIdType::Nep245`/`TokenIdType::Imt` whenever the per-intent delta magnitude is `<= 1`. Since fees are computed independently for each `TokenDiff` intent in a batch rather than aggregated per `TokenId` across the whole signed `DefuseIntents`/`MultiPayload`, an attacker can split one logical N-unit MT/Imt transfer into N intents of delta `-1` each, reducing total collected fees from `Pips::fee_ceil(protocol_fee, N)` to `0`.

### Finding Description
The broken binding: `sum(fees_collected[T])` over a batch should equal `Pips::fee_ceil(protocol_fee, aggregate_negative_delta(T))`, but instead equals `sum over each individual TokenDiff intent of Pips::fee_ceil(protocol_fee_masked_by_token_fee(delta_i), |delta_i|)`. Because `TokenDiff::token_fee` (lines 206-217) special-cases `amount <= 1` to `Pips::ZERO` for `Nep245`/`Imt`, and this is evaluated per-`TokenDiff` execution (`token_diff.rs` lines 70-72) inside the `DefuseIntents::execute_intent` loop (`intents/mod.rs` lines 108-113) with no cross-intent bookkeeping of the same `TokenId`, an attacker who wants to trade away N units of an Imt/Nep245 token can encode it as N separate `Intent::TokenDiff` entries each with `diff: {T: -1}` inside one signed `DefuseIntents` payload (single nonce/signature) instead of one `TokenDiff` with `diff: {T: -N}`. Each of the N legs independently computes `fee = token_fee(T, 1, protocol_fee).fee_ceil(1) = 0`. The counter-legs (the `+1` deposits needed to keep `TransferMatcher::finalize` balanced) can be supplied by a colluding/self-controlled counterparty account performing symmetric unit `TokenDiff`s, or bundled into the same batch if the attacker also controls the receiving side.

### Impact Explanation
`fee_collector` under-collects the protocol fee entirely (100% fee bypass) on any Nep245/Imt trade that can be restructured into unit legs, matching the "protocol fees bypassed" Critical category. This is repeatable per trade, per token, per account pair, with no cap — any signer performing MT/Imt swaps can always split into unit legs at no extra cost beyond gas/transaction overhead.

### Likelihood Explanation
Preconditions are trivial and match an ordinary unprivileged user: own units of an Nep245/Imt token, `protocol_fee > 0`, and a counterparty (possibly self via a second account) willing to match unit deltas. No special role, key, or contract state is needed — just constructing a `DefuseIntents` payload with N `TokenDiff` intents instead of 1, all under a single signature/nonce. This is realistically always exploitable whenever MT/Imt fees are configured, since nothing prevents batching arbitrarily many `Intent::TokenDiff` entries in one payload.

### Recommendation
Aggregate deltas per `TokenId` across all `TokenDiff` intents within a single `DefuseIntents`/`MultiPayload` execution (or across the whole matched trade group) before applying `token_fee`, rather than evaluating `token_fee` per individual intent instance. Alternatively, compute the fee-eligibility threshold from the net signer-level delta per token per transaction rather than per-intent delta magnitude.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or a near-workspaces sandbox test)
// 1. Construct signer A with N units of an Imt/Nep245 TokenId T, protocol_fee = Pips::ONE_PERCENT (or any > 0).
// 2. Case single-leg: DefuseIntents { intents: [TokenDiff { diff: {T: -N}, .. }] } executed via Engine;
//    assert fees_collected[T] == Pips::ONE_PERCENT.fee_ceil(N) > 0.
// 3. Case split-leg: DefuseIntents { intents: [TokenDiff{diff:{T:-1}}; N] } (matched by N corresponding
//    +1 deposits from a counterparty B in the same or paired batch so TransferMatcher::finalize succeeds);
//    assert total fees_collected[T] across all N executions == 0.
// 4. Assert case (3)'s total fee (0) < case (2)'s fee (Pips::ONE_PERCENT.fee_ceil(N)), demonstrating
//    the fee bypass for the same net token movement of N units.
```

### Citations

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-79)
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
        }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
        }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-217)
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
}
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L260-284)
```rust
    #[inline]
    pub fn add_delta(&mut self, owner_id: AccountId, token_id: TokenId, delta: i128) -> bool {
        self.0.entry_or_default(token_id).add_delta(owner_id, delta)
    }

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
}
```
