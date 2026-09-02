This confirms each `MultiPayload`/`TokenDiff` intent is processed fully independently — `execute_signed_intent` calls `intents.execute_intent` per payload with no cross-payload aggregation of deltas before fee calculation [1](#0-0) , and fee is computed per-intent from `Self::token_fee(token_id, amount, protocol_fee)` where `amount` is the `unsigned_abs()` of that single intent's delta [2](#0-1) .

### Title
Protocol fee bypass via splitting NEP-245/IMT `TokenDiff` transfers into `amount == 1` chunks - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` unconditionally returns `Pips::ZERO` for `TokenIdType::Nep245`/`TokenIdType::Imt` whenever the per-intent `amount <= 1`, and fees are computed strictly per-intent with no aggregation across a batch or across separately signed intents. An attacker holding N units of a fungible-style NEP-245/IMT token can move the entire balance by signing N separate `TokenDiff` intents of `delta == -1` each, paying zero protocol fee overall instead of `protocol_fee.fee_ceil(N)`.

### Finding Description
The broken binding: fee owed on a net negative delta of `-N` for a NEP-245/IMT token id should equal `protocol_fee.fee_ceil(N)`, but the sum of fees collected across N separately-submitted `-1` intents on the same token id is `0` (since `token_fee` returns `Pips::ZERO` whenever `amount <= 1`) [3](#0-2) .

Root cause: `execute_intent` computes `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` independently for each `(token_id, delta)` pair within a single `TokenDiff`, and `fees_collected` is scoped to that one intent's execution, never carried or aggregated across multiple signed intents in the batch [4](#0-3) . Each `MultiPayload` in a batch is independently verified and executed via `execute_signed_intent`, with no merging of deltas for fee purposes across payloads [5](#0-4) .

Exploit flow: attacker owns 1000 units of a NEP-245 token id inside the Verifier. Instead of signing one `TokenDiff { diff: {nep245_token: -1000} }` (which would incur `protocol_fee.fee_ceil(1000)`), the attacker signs 1000 separate `MultiPayload`s, each a `TokenDiff { diff: {nep245_token: -1} }`. Each execution takes the `amount > 1` branch check, finds `amount == 1`, hits the `TokenIdType::Nep245 | TokenIdType::Imt` fallthrough arm, and returns `Pips::ZERO`, so `fee_ceil(1) == 0` every time. After all 1000 intents execute, `fees_collected` was empty in every call, so `fee_collector`'s balance for that token id is never credited via `internal_add_balance` [6](#0-5) , despite the attacker having moved the full 1000 units out.

No existing guard prevents this: nonce/signature verification, deadline checks, and balance invariant finalization (`Deltas::finalize`) all operate correctly per-intent and across the batch net-zero requirement for token conservation, but none of them re-derive or enforce a fee based on the aggregate negative delta per token id across a batch.

### Impact Explanation
The `fee_collector` account is under-collected: it should receive `protocol_fee.fee_ceil(1000)` in NEP-245/IMT token fee revenue for this batch but receives `0`. This is repeatable by any signer for any NEP-245/IMT token id and any position size, scaling linearly with the number of separately-signed 1-unit intents (bounded only by batch/gas practicalities, not by any protocol control). This matches the Critical category "protocol fees bypassed."

### Likelihood Explanation
Preconditions are trivial for any unprivileged account: hold a NEP-245/IMT balance inside the Verifier, and a nonzero `engine.state.fee()` must be configured (default protocol behavior when fees are enabled). The attacker only needs to sign N small payloads with their own key rather than one large payload — this requires no special role, no relayer key, and no interaction with any restricted function. Cost scales with the number of intents/transactions (gas), but there is no rate limit or per-intent minimum fee floor preventing it.

### Recommendation
Base the fee exemption on something other than a purely per-intent, per-call `amount <= 1` check that can be trivially defeated by batching down to unit-sized deltas — e.g., track/aggregate the net negative delta per `(signer, token_id)` across the whole `MultiPayload` batch (or session) before applying the `amount <= 1` exemption, or remove the exemption for token ids that are not verifiably non-fungible (e.g., require an explicit NFT-classification of NEP-245 token ids rather than inferring it from a single call's `amount`).

### Proof of Concept
In `tests/src/tests/defuse/intents/token_diff.rs` (or a new test file), add a `near-workspaces`/sandbox test that:
1. Deploys a NEP-245 (multi-token) contract and deposits 1000 units of a single token id into the Verifier for a test user, with `Env::builder().fee(Pips::ONE_PERCENT)` (nonzero fee).
2. Signs and executes one `MultiPayload` containing `TokenDiff { diff: {nep245_token: -1000} }` on a fresh, separate account/token id, and records `TokenDiffEvent::fees_collected` for that token — assert it equals `protocol_fee.fee_ceil(1000)`.
3. On another fresh account/token id with the same starting balance, signs 1000 separate `MultiPayload`s each `TokenDiff { diff: {nep245_token: -1} }`, executes them all via `execute_intents`, and sums `TokenDiffEvent::fees_collected` across all 1000 emitted events.
4. Assert the sum from step 3 equals `0` while the value from step 2 is `protocol_fee.fee_ceil(1000) > 0`, demonstrating the two amounts diverge for economically identical transfers, and that `fee_collector`'s final NEP-245 balance differs between the two scenarios despite the same net token transfer.

### Citations

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
