## Finding: Valid

Analysis confirms a genuine per-intent fee-computation flaw for `Nep245`/`Imt` legs in `TokenDiff`.

**Binding claimed vs. actual code:**
`fees_collected[T] == Pips::fee_ceil(protocol_fee, Σ|negative_delta(T)| over the whole batch)`

Actual behavior in `TokenDiff::execute_intent`:
```
fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)
```
where `amount` is the magnitude of **that single intent's** delta for `token_id`, not the batch aggregate. `Self::token_fee` explicitly zeroes the fee for `Nep245`/`Imt` when `amount <= 1`. [1](#0-0) [2](#0-1) 

**Batch processing confirms no cross-intent aggregation of the fee base**: `execute_signed_intents` loops over each `MultiPayload`/intent independently, calling `TokenDiff::execute_intent` per intent, then only nets *balances* (not fee bases) via `TransferMatcher::finalize`, which only checks that total deltas sum to zero per token — it has no visibility into, or influence on, the fee already computed and credited per-intent. [3](#0-2) [4](#0-3) 

So an attacker can sign N `TokenDiff` intents, each with a single Nep245 (or Imt) leg of `delta == -1` on the same `token_id`, paired against one (or more) counterparty intents providing the offsetting `+N` deltas needed to satisfy the batch-wide netting invariant. Each of the N intents independently hits the `amount > 1` guard's `false` branch and returns `Pips::ZERO`, so `fees_collected` for that token stays 0 across all N intents, while the batch moves an aggregate of `N` units of that Nep245 token between accounts.

### Title
Protocol fee bypass on Nep245/Imt tokens via unary-delta intent splitting - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives fees for `Nep245`/`Imt` tokens whenever the delta magnitude of **that single intent** is `<= 1`. Because `TokenDiff::execute_intent` computes and collects fees per-intent rather than per-batch or per-account aggregate, an attacker can split one large Nep245 transfer into many `-1`-delta `TokenDiff` intents submitted in the same `execute_intents` batch, moving an arbitrarily large amount of the token while paying zero protocol fee.

### Finding Description
The broken equality: fees credited to `fee_collector` for token `T` should reflect `Pips::fee_ceil` applied over the total negative delta of `T` moved in the batch, but the code computes it independently per `TokenDiff` intent using only that intent's own `delta.unsigned_abs()`: [5](#0-4) 

`token_fee` treats any Nep245/Imt delta of magnitude `<=1` as fee-exempt (intended to avoid a 100%-of-single-unit fee on true NFT-like atomic transfers): [6](#0-5) 

An attacker who wants to move `N` units of a fungible-like Nep245 sub-token without paying fee simply signs `N` separate `TokenDiff` intents, each `{token_id: -1, other_token: +k}` (with unique nonces), and submits them all in one `MultiPayload` batch alongside a counterparty's offsetting intent(s) that net the batch to zero via `TransferMatcher::finalize`. Each of the `N` intents independently evaluates `token_fee(token_id, 1, fee) == Pips::ZERO`, so `fees_collected` is 0 for every intent, and the fee collector never receives a credit despite `N` units having moved. A single intent of `-N` on the same token would have incurred `fee.fee_ceil(N) > 0`. No existing guard (nonce/signature checks, `TransferMatcher::finalize`, `#[pause]`) inspects the aggregate magnitude of same-token/same-batch deltas across multiple intents; the invariant check only ensures balances net to zero, not that fees were charged proportionally to volume.

### Impact Explanation
The fee collector's balance for the targeted Nep245/Imt token permanently under-collects relative to actual trading volume routed through `TokenDiff`. This is a protocol-fee-bypass, matching the Critical category "protocol fees bypassed or over-collected." It is fully repeatable for any Nep245/Imt token and any account willing to sign more, smaller intents instead of one large intent, and can be automated by any relayer/bot forming batches.

### Likelihood Explanation
No privileged role is required. The attacker only needs: (1) a counterparty (which can be another account they control) willing to provide the offsetting deltas within the same batch, (2) enough NEAR gas to execute `N` intents in one `execute_intents` call, and (3) the ability to sign `N` payloads (trivial, off-chain, free). The cost scales with gas for `N` intents but the incentive (bypassing `protocol_fee` entirely) scales with the traded value, making it economically attractive for higher-value Nep245 transfers.

### Recommendation
Compute the Nep245/Imt fee-exemption threshold and fee amount based on the aggregate negative delta for a given `(signer_id, token_id)` pair across the whole batch (e.g., accumulate per-token per-signer negative deltas before applying `token_fee`, similar to how `TransferMatcher` aggregates transfers), rather than per individual `TokenDiff` intent.

### Proof of Concept
```rust
// cargo test in tests/src/tests/defuse/intents/token_diff.rs style
// 1. Set protocol fee > 0 (e.g. Pips::ONE_PERCENT).
// 2. Deposit N units of a Nep245 token to attacker_a; deposit matching offsetting
//    fungible token to attacker_b (or same attacker's 2nd account).
// 3. attacker_a signs N TokenDiff intents, each:
//    { diff: { nep245_token_id: -1, other_ft: +k }, ... } with distinct nonces.
// 4. attacker_b signs 1 (or more) TokenDiff intent(s) offsetting:
//    { diff: { nep245_token_id: +N, other_ft: -k*N }, ... }
// 5. Batch all N+1 signed payloads into one `execute_intents` call.
// 6. Assert: batch succeeds (TransferMatcher balances to zero).
// 7. Assert: mt_balance_of(fee_collector, nep245_token_id) == 0
//    despite N units of nep245_token_id having moved between attacker_a and attacker_b.
// 8. Contrast: signing ONE TokenDiff with delta = -N on the same token would have
//    yielded fees_collected[nep245_token_id] == fee.fee_ceil(N) > 0.
```

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-79)
```rust
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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-284)
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
}
```
