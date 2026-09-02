### Title
Fee bypass on NEP-245/IMT `TokenDiff` transfers by splitting a single-token delta across multiple intents - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes the fee for a negative delta by calling `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` using only the `amount` present *within that one intent*, not the aggregate net delta for that `TokenId` across the whole `MultiPayload` batch. Because `TokenIdType::Nep245 | TokenIdType::Imt` are fee-exempt when `amount <= 1`, an unprivileged signer can split any semi-fungible transfer of `amount == N > 1` into `N` separate `TokenDiff` intents of `amount == 1` each (matched by an equally split counterparty side, satisfying `TransferMatcher::finalize`), reducing the total fee collected from `protocol_fee.fee_ceil(N)` to `0`.

### Finding Description
The binding that should hold is: for a fixed net negative delta `D` on a `Nep245`/`Imt` `TokenId` within one settled batch, `fee_collected(token_id) == protocol_fee.fee_ceil(|D|)` regardless of how the signer partitions `D` across `TokenDiff` intents in that batch.

The actual code in `TokenDiff::execute_intent` [1](#0-0)  iterates only over `self.diff` (a single intent's `BTreeMap<TokenId, i128>`), and for each negative delta computes `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` where `amount` is that single intent's `unsigned_abs()` delta. `Self::token_fee` explicitly waives the fee for `Nep245`/`Imt` when `amount <= 1`: [2](#0-1) 

There is no cross-intent aggregation step for fee purposes: `Engine::execute_signed_intents` simply loops over each signed payload/intent and calls `execute_intent` independently [3](#0-2) ; `DefuseIntents::execute_intent` likewise iterates each `Intent` and executes it standalone [4](#0-3) . The only cross-intent invariant enforced is that deposits/withdrawals net to zero per token via `TransferMatcher::finalize` [5](#0-4)  — this constrains the *sum of deltas* to balance, but says nothing about the fee, which is computed and credited per-intent, per-leg, before `finalize()` even runs.

Exploit flow: an unprivileged signer (or two colluding/self-owned accounts, Alice and Bob) wanting to move `amount=2` of a `Nep245` token from Alice to Bob can either:
- (a) sign one `TokenDiff{diff: {token_id: -2}}` from Alice matched by one `TokenDiff{diff: {token_id: +2}}` from Bob → `token_fee` sees `amount=2 > 1` → fee = `protocol_fee.fee_ceil(2)`, credited to `fee_collector`.
- (b) sign two `TokenDiff{diff: {token_id: -1}}` intents from Alice matched by two `TokenDiff{diff: {token_id: +1}}` intents from Bob, all included in the same call to `execute_intents` → each execution sees `amount=1`, `token_fee` returns `Pips::ZERO` → total fee = `0`.

Both (a) and (b) produce the identical net token movement (Alice -2, Bob +2, `TransferMatcher` nets to zero and finalizes successfully), yet the fee credited to `fee_collector` differs: `Pips::fee_ceil(2)` vs `0`. No signature, nonce, salt, or lock-state check (`MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, `MaybeLegacyNonces::commit`, `Lock::get_mut`) constrains fee computation or prevents this partition; they only gate authorization/replay, not fee aggregation.

### Impact Explanation
The Verifier under-collects protocol fees on every `Nep245`/`Imt` transfer whose signer chooses to split it into unit legs, matching the "protocol fees bypassed or over-collected" Critical category. This is not limited to a specific pair of accounts or token — any account holding `>=2` units of any NEP-245/IMT `TokenId` inside the Verifier can apply this trick repeatedly and unboundedly, on any batch, at zero additional cost (the attacker still needs a counterparty leg summing to the same net delta, but that counterparty can be another account fully controlled by the same attacker). No victim funds are stolen from another user directly, but the protocol's intended fee revenue is systematically avoidable, which is an economic Critical-severity flaw in fee collection as scoped by the rules.

### Likelihood Explanation
Preconditions are trivial: an account must simply hold `>=2` units of a `Nep245`/`Imt` token inside the Verifier (deposited normally) and have a counterparty (which can be another self-controlled account) willing/able to receive the split legs. No privileged role, relayer key, or DAO action is required — only ordinary `execute_intents` calls with self-crafted `MultiPayload`s. The attack is fully repeatable across all NEP-245/IMT tokens and all batch sizes, at essentially zero marginal cost beyond signing a few extra intents.

### Recommendation
Aggregate the total negative delta per `TokenId` across the entire batch (or at minimum across all `TokenDiff` intents from every payload processed in `execute_signed_intents`) before evaluating `TokenIdType::Nep245 | TokenIdType::Imt` fee-exemption thresholds, rather than evaluating `amount > 1` on a per-intent-leg basis. Concretely, compute `token_fee` inputs from a pre-pass that sums `unsigned_abs()` negative deltas per `TokenId` across all intents in the call to `execute_intents`, then apply `fee_ceil` on that aggregate before crediting `fee_collector`, or reject batches where the same `TokenId` appears with negative delta split across more than one `TokenDiff` intent for the same signer.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or as an integration test using near-workspaces sandbox)
// Scenario A: single TokenDiff intent moving amount=2 of a Nep245 token
//   Alice signs TokenDiff{ diff: {nep245_token_id: -2} }
//   Bob   signs TokenDiff{ diff: {nep245_token_id: +2} }
//   execute_intents([alice_payload, bob_payload])
//   assert_eq!(fee_collector_balance(nep245_token_id), protocol_fee.fee_ceil(2));

// Scenario B: same net movement split into two unit legs
//   Alice signs TokenDiff{ diff: {nep245_token_id: -1} } (payload 1)
//   Alice signs TokenDiff{ diff: {nep245_token_id: -1} } (payload 2)
//   Bob   signs TokenDiff{ diff: {nep245_token_id: +1} } (payload 3)
//   Bob   signs TokenDiff{ diff: {nep245_token_id: +1} } (payload 4)
//   execute_intents([payload1, payload2, payload3, payload4])
//   assert_eq!(fee_collector_balance(nep245_token_id), 0);

// Assert the divergence directly:
// assert_ne!(fee_collector_balance_scenario_a, fee_collector_balance_scenario_b);
// where scenario_a == protocol_fee.fee_ceil(2) > 0 and scenario_b == 0,
// despite identical net Alice/Bob balance changes in both scenarios.
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
