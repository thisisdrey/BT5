### Title
Protocol fee bypassed on `Nep245`/`Imt` tokens via intent/leg splitting (`|delta| <= 1` exemption is evaluated per-`TokenDiff` intent, not per net signer exposure) - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::execute_intent` computes the fee for a negative delta using `Self::token_fee(token_id, amount, protocol_fee)`, where `amount` is the magnitude of that single intent's own delta on that token. For `Nep245`/`Imt` token types, `token_fee` returns `Pips::ZERO` whenever `amount <= 1`. Because fee assessment is per-`TokenDiff`-entry rather than per net token exposure across a batch (or even across multiple entries in the same signed message), a signer can split what would otherwise be a single `delta == -N` movement into `N` separate `TokenDiff` entries each with `delta == -1`, paying zero fee on the entire volume instead of `Pips::fee_ceil(N, protocol_fee)`.

### Finding Description
The broken binding: for a `Nep245`/`Imt` token `T`, the protocol expects `fee_collector` to receive `Pips::fee_ceil(protocol_fee, |net negative exposure of signer on T|)`. Instead, the code computes and sums `Pips::fee_ceil(protocol_fee, |delta_i|)` independently for each `TokenDiff` entry `i`, i.e. `Σ token_fee(T, |delta_i|, fee).fee_ceil(|delta_i|) ≠ token_fee(T, Σ|delta_i|, fee).fee_ceil(Σ|delta_i|)` whenever the per-entry split keeps every `|delta_i| <= 1`.

Code path: `ExecutableIntent for DefuseIntents::execute_intent` iterates every intent in a signed message and executes each independently [1](#0-0) . `TokenDiff::execute_intent` then computes, per `TokenDiff` value, `fees_collected` from `Self::token_fee(token_id, amount, protocol_fee)` using only that intent's own `delta.unsigned_abs()` [2](#0-1) . The root cause is `token_fee`'s explicit exemption: `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}` else `return Pips::ZERO` for NFTs/MTs with `|delta| <= 1` [3](#0-2) . This exemption is evaluated strictly against the amount carried by that one `TokenDiff.diff` entry (a `BTreeMap<TokenId, i128>`, so at most one entry per token per `TokenDiff`) [4](#0-3) , with no aggregation across multiple `TokenDiff` intents in the same message or across the batch.

Exploit flow: an unprivileged signer with sufficient balance signs one `MultiPayload` (or two) containing multiple `TokenDiff` intents, each moving `delta == -1` on the same `Nep245`/`Imt` token id `T` (with matching positive legs elsewhere in the batch to satisfy `TransferMatcher::finalize` net-zero invariant, e.g. from a counterparty solver also willing to trade in unit legs) [5](#0-4) . Each `TokenDiff::execute_intent` call independently sees `amount == 1`, so `Self::token_fee` returns `Pips::ZERO` and `fees_collected` stays empty for every leg, even though the signer's aggregate exposure on `T` equals what a single `delta == -N` intent would have been fee-liable for. `engine.state.internal_add_balance(fee_collector, fees_collected)` is then skipped entirely (`fees_collected.is_empty()`) [6](#0-5) .

Existing guards (`MultiPayload::verify`, nonce/salt checks in `Engine::execute_signed_intent`, `TransferMatcher::finalize`) validate signatures, replay-protection and that the batch nets to zero in token units, but none of them re-derive or reconcile fee amounts against aggregate exposure per token across the intents in a batch [7](#0-6) . `Engine::execute_signed_intents` simply loops over every signed payload and calls `finalize()` once at the end for solvency, not for fee re-computation [8](#0-7) .

### Impact Explanation
`fee_collector`'s expected revenue on `Nep245`/`Imt` token trades is under-collected (down to zero) whenever the signer structures their `TokenDiff` diffs as a sequence of unit legs instead of one aggregate delta. This falls under the explicitly listed Critical category "protocol fees bypassed or over-collected." The bypass is fully repeatable per token id, per batch, and across accounts — any signer trading `Nep245`/`Imt` assets can apply this technique indefinitely, at the cost of extra signed intents/gas only. It does not affect `Nep141` fungible tokens, since `token_fee` never exempts `TokenIdType::Nep141` regardless of amount [3](#0-2) , so the blast radius is scoped to multi-token/IMT balances, but within that scope the fee can be reduced to zero on arbitrarily large aggregate volume.

### Likelihood Explanation
Preconditions are minimal and fully attacker-controlled: the signer needs enough balance to cover the legs (same total balance as the single non-split intent would require) and a counterparty willing to match in unit-sized legs (a normal solver/RFQ flow, or the signer's own second account acting as counterparty). No DAO/role, relayer key, or upgrade access is required. `engine.state.fee() > 0` is the only environmental precondition, which is the normal operating state whenever fees are enabled. Cost to the attacker is simply constructing multiple `TokenDiff` entries (either in one signed message or across multiple signed payloads) instead of one — cheap and fully repeatable.

### Recommendation
Aggregate fee computation per token across all `TokenDiff` intents belonging to the same signer within a batch (or at minimum within the same signed message) before applying the `Nep245`/`Imt` `amount <= 1` exemption, e.g. accumulate net negative exposure per `(signer_id, token_id)` first, then compute `token_fee`/`fee_ceil` once on the aggregated magnitude, rather than per individual `TokenDiff.diff` entry.

### Proof of Concept
Add a `cargo test` in `contracts/defuse/core/src/intents/token_diff.rs` (or `tests/src/tests/defuse/intents/token_diff.rs` using the sandbox) that:
1. Builds `MultiPayload` A: a single signer with one `TokenDiff` of `delta == -2` on a `Nep245` token id `T` (plus a matching positive counterpart to satisfy `TransferMatcher::finalize`), with `protocol_fee > 0`. Executes via `execute_signed_intents`/`execute_intents`, then asserts `fee_collector`'s balance on `T` increased by `Pips::fee_ceil(protocol_fee, 2)`.
2. Builds `MultiPayload` B: same signer/net exposure, but expressed as two `TokenDiff` intents (or one message with two `TokenDiff` entries) each with `delta == -1` on `T`, with matching counterpart legs. Executes the batch, then asserts `fee_collector`'s balance on `T` is unchanged (`0` fee collected).
3. Assert that scenario A's collected fee differs from (is strictly greater than) scenario B's collected fee despite identical net signer balance change on `T`, proving the fee-bypass via splitting.

### Citations

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L18-18)
```rust
pub type TokenDeltas = Amounts<BTreeMap<TokenId, i128>>;
```

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-40)
```rust
    pub fn execute_signed_intents(
        mut self,
        signed: impl IntoIterator<Item = MultiPayload>,
    ) -> Result<Transfers> {
        for signed in signed {
            self.execute_signed_intent(signed)?;
        }
        self.finalize()
    }
```

**File:** contracts/defuse/core/src/engine/mod.rs (L42-83)
```rust
    fn execute_signed_intent(&mut self, signed: MultiPayload) -> Result<()> {
        // verify signed payload and get public key
        let public_key = signed.verify().ok_or(DefuseError::InvalidSignature)?;

        // calculate intent hash
        let hash = signed.hash();

        // extract NEP-413 payload
        let DefusePayload::<DefuseIntents> {
            signer_id,
            verifying_contract,
            deadline,
            nonce,
            message: intents,
        } = signed.extract_defuse_payload()?;

        // check recipient
        if verifying_contract != *self.state.verifying_contract() {
            return Err(DefuseError::WrongVerifyingContract);
        }

        self.inspector.on_deadline(deadline);

        // make sure message is still valid
        if deadline < Timestamp::now() {
            return Err(DefuseError::DeadlineExpired);
        }

        // make sure the account has this public key
        if !self.state.has_public_key(&signer_id, &public_key) {
            return Err(DefuseError::PublicKeyNotExist(signer_id, public_key));
        }

        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
    }
```
