### Title
Protocol fees bypassed on Nep245/IMT `TokenDiff` transfers by splitting into multiple amount=1 intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` explicitly returns `Pips::ZERO` for `Nep245`/`Imt` tokens whenever the per-intent transfer amount is `<= 1`, and this exemption is evaluated independently for each `TokenDiff` intent as it is executed rather than being aggregated over the whole signed batch or even the whole signer/token pair. An attacker who wants to move `N` units of a multi-token (Nep245) or IMT token can split a single `TokenDiff{delta: -N}` into `N` separate `TokenDiff{delta: -1}` intents (matched by any counterparty providing the offsetting `+1` legs, e.g. the attacker's own second account) and pay zero fee in total, whereas doing it as one `TokenDiff{delta: -N}` intent (`N>1`) would have incurred `fee_ceil(fee, N) > 0`.

### Finding Description
Binding claimed: `fees credited to fee_collector for token T over the whole batch == Pips::fee_ceil(fee, total_negative_delta_of_T_in_batch)`. Actual: fees credited `== Σ over each TokenDiff intent i of Pips::fee_ceil(fee, |delta_i|)`, and for `Nep245`/`Imt` tokens, `token_fee` returns `Pips::ZERO` whenever `|delta_i| <= 1` regardless of how many such intents accumulate to the same token in the batch: [1](#0-0) 

The fee is computed and collected purely inside a single `TokenDiff::execute_intent` call over its own `diff` map: [2](#0-1) 

`execute_intents` -> `execute_signed_intents` iterates every signed `MultiPayload`/`DefuseIntents`, and each contained `Intent::TokenDiff` is dispatched and executed independently by `DefuseIntents::execute_intent`, with no cross-intent or cross-payload fee aggregation: [3](#0-2) [4](#0-3) 

The only cross-intent invariant enforced across the whole batch is `TransferMatcher::finalize`, which only checks that deposits and withdrawals of each `TokenId` net to zero (no funds materialize out of nothing); it does **not** verify or re-derive expected fee amounts: [5](#0-4) 

Exploit flow: the attacker (owning `>=2` units of some `Nep245`/`Imt` `token_id`, possibly split across two accounts they control) signs one `MultiPayload` containing `N` `TokenDiff` intents, each with `diff = {token_id: -1, other_token: +k}`, matched by `N` counter-leg `TokenDiff` intents (from a second attacker-controlled account or colluding counterparty) each with `diff = {token_id: +1, other_token: -k}`. Each of the `2N` `TokenDiff::execute_intent` calls independently evaluates `token_fee(token_id, 1, fee)`, which hits the `TokenIdType::Nep245 | TokenIdType::Imt` arm with `amount == 1`, returning `Pips::ZERO`, so `fee_ceil(1) == 0` every time. The aggregate fee collected for the whole `token_id` transfer of size `N` is `0`, whereas a single `TokenDiff{delta: -N}` intent (`N > 1`) would have taken `Self::token_fee(token_id, N, fee).fee_ceil(N) > 0` under the same protocol fee `fee > 0`. `TransferMatcher::finalize` still succeeds because the `N` unit deposits/withdrawals of `token_id` net to zero across the batch, so no `InvariantViolated` error is raised — none of the existing guards (`MultiPayload::verify`, nonce/salt checks, `TransferMatcher::finalize`) detect or prevent the fee shortfall, because none of them re-derive or compare an expected aggregate fee.

### Impact Explanation
`fee_collector`'s balance for that `Nep245`/`Imt` `token_id` is under-collected relative to what a single equivalent-size `TokenDiff` would produce, matching the "protocol fees bypassed" Critical category from the rules. The bypass is fully repeatable: any unprivileged account holding `>=2` units of any `Nep245`/`Imt` token, or coordinating with a colluding/self-owned counterparty account, can apply this trick to any volume by increasing `N`, always paying zero fee on the `Nep245`/`Imt` leg regardless of total size moved. Blast radius is scoped to `Nep245`/`Imt` fee revenue only (Nep141 fungible tokens are unaffected since `token_fee` charges fee unconditionally for `TokenIdType::Nep141`).

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's control: own or control two accounts, hold `>=2` units of a Nep245/IMT token, and be able to sign/submit a `MultiPayload` with `N` matched `TokenDiff` intents (which the attacker can construct arbitrarily since `execute_intents`/`simulate_intents` accept any signed batch). Cost scales with `N` (gas for more intents in the payload), but the fee savings also scale with `N`, making the exploit worthwhile for any transfer where `fee * amount` exceeds the marginal gas cost of extra intents. Fully repeatable across accounts, tokens, and batches.

### Recommendation
Aggregate fee computation per `(signer_id/token_id)` (or globally per `token_id`) across the whole batch before applying the `amount <= 1` Nep245/IMT exemption, e.g. compute the threshold check against the total negative delta accumulated for that `token_id` across all `TokenDiff` intents processed by `TransferMatcher`/`Deltas`, rather than per individual intent's own `diff` map entry. Alternatively, remove or tighten the `amount <= 1` exemption so it cannot be gamed by chunking (e.g., only exempt truly atomic `Nep171` NFTs, and always charge the configured fee on `Nep245`/`Imt` deltas regardless of size, since these are typically fungible-like balances).

### Proof of Concept
```rust
// cargo test in tests/src/tests/defuse/intents/token_diff.rs (near-workspaces sandbox)
// Setup: deploy an Nep245 mt contract, deposit 5 units of token_id "X" to attacker_a.
// Also deposit some Nep141 "pay" token to attacker_b for the counter-leg.

// Case 1: single TokenDiff moving 5 units at once (fee > 0 config)
// attacker_a signs: diff = { X: -5, pay: +P }
// attacker_b signs: diff = { X: +5, pay: -P' }  (P' accounts for TokenDiff::closure_delta with fee)
// execute_intents(...) -> assert mt_batch_balance_of(fee_collector, [X]) > 0

// Case 2: same total volume (5 units), split into 5 intents of amount 1 each, same MultiPayload
// attacker_a signs 5x: diff = { X: -1, pay: +p }
// attacker_b signs 5x: diff = { X: +1, pay: -p }  (no fee-adjusted closure needed since token_fee(X,1,fee)==0)
// execute_intents(...) -> assert mt_batch_balance_of(fee_collector, [X]) == 0

// Assertion demonstrating the broken binding:
// fee_collector balance for X in Case 1 (single intent, amount=5) != fee_collector balance for X in Case 2 (5x amount=1 intents)
// despite both moving the same total 5 units of the same Nep245 token_id in one batch.
```

### Citations

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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-83)
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
