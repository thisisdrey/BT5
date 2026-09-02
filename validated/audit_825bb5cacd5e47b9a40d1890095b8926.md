### Title
Fee bypass on NEP-245/IMT tokens by splitting a `TokenDiff` delta into multiple `|delta| == 1` legs - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` returns `Pips::ZERO` for any `Nep245`/`Imt` leg whose `amount <= 1`, and `TokenDiff::execute_intent` computes and collects fees strictly per-intent, per-token-leg, with no aggregation across intents in a batch. An unprivileged signer can therefore split a large negative delta on a NEP-245/IMT token into `N` separate `TokenDiff` intents each with `delta == -1` and pay zero protocol fee in total, whereas submitting the same `-N` delta as one leg would incur `protocol_fee.fee_ceil(N)`.

### Finding Description
The intended (and reportedly assumed) binding is:

`fees_collected(fee_collector, T) == Pips::fee_ceil(protocol_fee, Σ|negative deltas of T in the batch|)`

The actual code never computes this aggregate. In `execute_intent`, the fee for a negative delta is computed independently per `TokenDiff` object: [1](#0-0) 

and `token_fee` explicitly zeroes the fee whenever the per-leg `amount <= 1` for `Nep245`/`Imt` (and always for `Nep171`): [2](#0-1) 

Each `TokenDiff` intent is executed inside its own signed `MultiPayload` with its own `intent_hash`/`nonce`, dispatched independently by `execute_signed_intent`/`DefuseIntents::execute_intent`: [3](#0-2) [4](#0-3) 

The only cross-intent check performed on the whole batch is that the sum of (fee-adjusted) supply deltas per token nets to zero (`Deltas::finalize`/invariant check, as exercised by the `invariant_violated` test). This check operates on whatever supply deltas the individual `TokenDiff`s produce; since fee is zero for each `|delta|==1` leg, the raw deltas need no fee adjustment to net to zero, so a batch of `N` unit legs (each paired with an offsetting counter-leg from the same or a colluding signer) balances trivially without ever triggering a nonzero fee.

Concretely: sign `N` distinct `MultiPayload`s (distinct nonces/intent hashes), each containing one `TokenDiff` with `{T: -1, U: +k}` for some paired token `U` (or pair them against another signer's `TokenDiff`s so the whole batch nets to zero), and submit them together via `execute_intents`. Every `token_fee(T, 1, protocol_fee)` call returns `Pips::ZERO`, so `fees_collected` for `T` is `0` for every leg, and the aggregate fee credited to `fee_collector` is `0`. Submitting the economically equivalent single `TokenDiff` with `{T: -N, U: +k*N}` instead calls `token_fee(T, N, protocol_fee)` with `amount = N > 1`, applying `protocol_fee` and yielding `fee_ceil(protocol_fee, N) > 0`.

None of the listed guards (`MultiPayload::verify`, `verify_intent_nonce`, `commit_nonce`, `SaltRegistry::is_valid`, `Lock`, `TransferMatcher::finalize`, `assert_one_yocto`, `#[pause]`, access-control guards, checked arithmetic) inspect or aggregate per-token deltas across distinct intents/payloads in a batch for fee purposes; they only guarantee signature validity, nonce freshness, and that the batch's net supply deltas balance to zero.

### Impact Explanation
The `fee_collector` is under-paid (protocol fee bypass) for any NEP-245 (`Nep245`)/IMT trade that a signer chooses to fragment into unit legs, while the signer/solver still obtains the full economic trade because the batch nets to zero as required by the invariant check. This is repeatable for any token id, any account, and any batch size (bounded only by gas/transaction size, which is explicitly out of scope to disqualify on its own). This matches the "protocol fees bypassed" Critical category since value (the fee) that should flow to `fee_collector` never leaves the trading parties' cumulative balance change.

### Likelihood Explanation
Preconditions are minimal and fully attacker-controlled: hold ≥1 unit of an NEP-245/IMT balance in the Verifier, a nonzero `protocol_fee` configured via `engine.state.fee()`, and the ability to sign multiple `MultiPayload`s with fresh nonces (trivial, self-controlled) and submit them in one `execute_intents`/`simulate_intents` call. No privileged role, relayer key, or third-party cooperation is required beyond a counterparty (or a second signer key of the same attacker) to provide the offsetting legs, exactly as ordinary P2P/solver trades already require.

### Recommendation
Aggregate negative deltas per `token_id` across all `TokenDiff` intents within the same execution (and ideally within the whole batch) before applying the `Nep245`/`Imt` `amount <= 1` fee exemption, or remove/tighten the exemption so it cannot be trivially defeated by amount-splitting (e.g., base the exemption on the token's per-unit indivisibility rather than the delta magnitude of an individual leg, or track fee-liable running totals per signer/token within `Deltas`/`Engine` state during a single `execute_signed_intents` call).

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or `tests/src/tests/defuse/intents/token_diff.rs` sandbox style):
1. Configure `protocol_fee = Pips::ONE_PERCENT` (or any nonzero fee).
2. Case A ("combined"): Execute one `TokenDiff` intent with `diff = {T: -N, U: +K}` (N>1, T is Nep245/Imt) paired with a counterparty `TokenDiff` `{T: +N, U: -K}`; assert `fees_collected.amount_for(&T) == protocol_fee.fee_ceil(N)` and `> 0`.
3. Case B ("split"): Execute `N` separate signed `TokenDiff` intents each `{T: -1, U: +K/N}` from the attacker plus matching counter-legs, submitted together in one `execute_intents([...])` call; assert the sum of `fees_collected.amount_for(&T)` across all `N` `TokenDiffEvent`s (or the observed increase in `fee_collector`'s `T` balance) is `0`.
4. Assert `0 < protocol_fee.fee_ceil(N)` to demonstrate `Σ(split fees) < fee_ceil(protocol_fee, Σ|deltas|)`, proving the binding is violated.

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
