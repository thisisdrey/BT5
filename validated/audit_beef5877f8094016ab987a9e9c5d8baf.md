Confirmed: `execute_signed_intents` processes each `MultiPayload` in the batch and calls `intents.execute_intent` per signed payload, then finalizes all accumulated deltas at once via `TransferMatcher::finalize`, which enforces net-zero balance across the *entire* batch per `TokenId` [1](#0-0) . Fees are computed strictly per intent, inside `TokenDiff::execute_intent`, using only that single intent's `delta.unsigned_abs()` as `amount` [2](#0-1) . `TokenDiff::token_fee` explicitly zeroes the fee for `Nep245`/`Imt`/`Nep171` tokens whenever `amount <= 1` [3](#0-2) . There is no batch-level or per-token aggregation of `amount` before this check, and `TransferMatcher::finalize` only cares about net-zero deltas, not about how fees were computed [4](#0-3) .

### Title
Protocol fee bypass via splitting large Nep245/Imt TokenDiff negative deltas into unit (-1) intents - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives the fee entirely for `Nep245`/`Imt` token deltas with `|amount| <= 1`, and `TokenDiff::execute_intent` computes this fee independently per signed intent rather than per aggregate token movement in the batch. An attacker can decompose a large negative delta on a single Nep245 `TokenId` into many `-1` legs across multiple signed `TokenDiff` intents in the same `execute_intents` batch, each evaluating `token_fee` to `Pips::ZERO`, while the batch as a whole still nets to a large `token_in` amount that would have incurred a nonzero fee had it been expressed as one intent.

### Finding Description
The broken binding: `fees credited to fee_collector for token T == Pips::fee_ceil(protocol_fee, Σ|negative deltas of T in the batch|)`. The actual code computes, for each `TokenDiff` intent independently:
```
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
where `amount = delta.unsigned_abs()` is scoped to that single intent's `diff` entry [5](#0-4) . `token_fee` returns `Pips::ZERO` whenever the `Nep245`/`Imt` amount is `<= 1` [3](#0-2) .

Exploit flow: attacker (using one or several accounts they control) signs N `MultiPayload`s (or N `TokenDiff` intents within payload(s)), each containing a single `TokenDiff` with one leg `{ Nep245TokenId(mt_contract, "X"): -1 }`, together with matching counter-legs so the whole batch nets to zero for every `TokenId` (required by `TransferMatcher::finalize`) [4](#0-3) . Because `execute_signed_intents` iterates and executes every signed payload before finalizing balances once at the end [6](#0-5) , the same net effect as a single `-N` `TokenDiff` (which would incur `fee_ceil(protocol_fee, N) > 0`) is achieved with zero fee collected, since each of the N `-1` legs is evaluated in isolation and exempted. No code path aggregates `amount` across intents/payloads before calling `token_fee`, and `MultiPayload::verify`, nonce/salt checks, and `assert_one_yocto` only guard authenticity/replay, not fee correctness.

### Impact Explanation
Protocol fee revenue on Nep245 (multi-token, which can represent fungible balances, not just NFTs) and Imt token trades is bypassed entirely for any size of trade, by an unprivileged attacker who only needs to structure their own settlement as many unit legs instead of one bulk leg. This matches the "protocol fees bypassed" Critical category in scope: the Verifier's `fee_collector` receives strictly less than the protocol fee schedule mandates, for every trade routed this way, with no cap on repeatability (any account, any Nep245/Imt token, any batch).

### Likelihood Explanation
No special privilege, balance, or role is required beyond controlling the Nep245/Imt tokens being swapped (which an unprivileged trader legitimately has). The attacker only pays extra gas/transaction overhead for signing and including N intents/payloads in one `execute_intents` call instead of one; this is a pure implementation/protocol-design gap, not requiring any other party's cooperation beyond the normal counterparty of the swap. It is trivially repeatable across accounts, tokens, and batches.

### Recommendation
Compute `token_fee`'s `amount` gate against the aggregate negative delta accumulated per `(signer, TokenId)` across the whole batch (e.g., accumulate deltas per token in `Deltas`/`TransferMatcher` before applying the `amount <= 1` exemption at `finalize()` time), or remove/tighten the `amount <= 1` fee exemption for `Nep245`/`Imt` token types so it cannot be trivially evaded by decomposition into unit legs.

### Proof of Concept
```
cargo test -p defuse-core token_diff_fee_bypass_via_unit_splitting -- --nocapture
```
Plan:
1. Set `protocol_fee = Pips::ONE_PERCENT` (or similar nonzero) in the engine `State`.
2. Create Nep245 `TokenId` `mt.near:X` and credit signer A with balance `100` of it (`internal_add_balance`).
3. Build a batch of 100 signed `MultiPayload`s from A, each a single `TokenDiff` `{ mt.near:X: -1, ft.near: TokenDiff::closure_delta(&ft, +1_per_unit_equivalent, fee) }` (or simpler: pair the -1 legs with matching +1 legs on the same MT token routed to a second attacker-controlled account B, keeping net zero across the batch).
4. Call `Engine::execute_signed_intents` with the 100 payloads.
5. Assert: `fees_collected` aggregated across `TokenDiffEvent`s for `mt.near:X` == `0`, while `fee_ceil(protocol_fee, 100)` (the fee that would be charged for one `-100` intent) is `> 0`.
6. Contrast with a second run using a single `TokenDiff` intent with `delta = -100` on the same token: assert `fees_collected` for `mt.near:X` == `fee_ceil(protocol_fee, 100) > 0`, demonstrating the discrepancy is caused strictly by decomposition, not by amount.

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
