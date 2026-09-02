## Title
Protocol fee entirely bypassed on NEP-245/IMT token legs by splitting a large-value transfer into repeated `|delta| == 1` `TokenDiff` intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

## Summary
`TokenDiff::token_fee` unconditionally returns `Pips::ZERO` for any `TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt` leg whose transferred `amount <= 1`, with no aggregation across intents in a batch or across a signer's session. Because `NEP-245`/`IMT` token ids can represent semi-fungible balances of arbitrarily high per-unit value (unlike true 1-of-1 NFTs), an unprivileged signer can move any quantity of such value through the protocol fee-free by splitting one large `TokenDiff` into N separate `amount == 1` `TokenDiff` intents.

## Finding Description
The broken binding: fee owed on a negative delta of a token should equal `fee * value_transferred_for_that_token` (per `TokenDiff::supply_delta`/`token_fee` intent). Instead, for `TokenIdType::Nep245`/`Imt`, `token_fee` returns `Pips::ZERO` whenever the per-intent `amount <= 1`: [1](#0-0) 

The fee is computed strictly *per `TokenDiff::execute_intent` call, per token_id, per that single intent's `unsigned_abs(delta)`*: [2](#0-1) 

`Engine::execute_signed_intents` accepts a `Vec<MultiPayload>` and processes each payload independently — verifying its own signature and committing its own (attacker-chosen) nonce — with no cross-intent aggregation of amounts for fee purposes: [3](#0-2) 

All the resulting per-intent balance deltas (regardless of how many separate intents produced them) are accumulated in the same `TransferMatcher` and net out to real token transfers at `finalize()`, matching senders to receivers by amount regardless of how many `TokenDiff` intents contributed to the total: [4](#0-3) 

Exploit: attacker (either alone using two accounts they control, or with a willing counterparty) constructs a `MultiPayload` batch containing:
- N `TokenDiff` intents, each signed with a distinct valid nonce by signer A, each with `diff = {T: -1, U: +u_i}` where `T` is a high-value-per-unit NEP-245/IMT token id.
- Matching `TokenDiff` intents from signer B with `diff = {T: +1, U: -u_i}` (NEP-141 `U`).

Each of the N `T`-side legs hits the `amount <= 1` branch and is charged `Pips::ZERO`, regardless of `T`'s real value; only the `U` (NEP-141) legs are charged the normal `fee`. After `TransferMatcher::finalize`, N units of `T` (which can represent large aggregate value) move between A and B with zero fee collected on the `T` side, whereas a single `TokenDiff` with `delta = -N` on `T` would have incurred `Self::token_fee(T, N, fee).fee_ceil(N)` (non-zero once `N > 1`).

No existing guard prevents this: `MultiPayload::verify`/nonce commitment only ensure each intent is validly signed and not replayed, not that per-token deltas are aggregated for fee purposes; `TransferMatcher::finalize` only enforces balance conservation, not fee correctness.

## Impact Explanation
`fee_collector` under-collects fees on any `NEP-245`/`IMT` transfer that a signer chooses to structure as a sequence of `amount == 1` `TokenDiff` legs instead of one bulk leg, for arbitrarily large aggregate value, as long as per-unit granularity of the token (e.g., a semi-fungible/high-value-per-unit multi-token) allows it. This is repeatable across any signer, any NEP-245/IMT token, and any batch size (bounded only by gas/tx size, which is out of scope). It matches the Critical category "protocol fees bypassed... against `fee_collector`."

## Likelihood Explanation
No special privilege, role, or victim key is required — only the ability to sign `MultiPayload`s and call `execute_intents`, which is available to any unprivileged account. The only precondition is holding (or trading) a NEP-245/IMT token whose unit value is economically significant, and being willing to split the transfer into N intents (linear cost in number of signatures/gas, which is explicitly out of scope per the rules but does not block feasibility for moderately-sized N).

## Recommendation
Compute/aggregate `TokenDiff` fees per signer/token across the whole batch (or across a signer's session) rather than per single intent's `amount`, so an attacker cannot bypass the `amount <= 1` fee exemption by splitting a larger transfer into many unit-sized `TokenDiff` intents. Alternatively, restrict the `amount <= 1` fee exemption to token ids that are provably always-unit-supply (true `NEP-171` NFTs), and apply proportional fee (with correct rounding, not full-unit consumption) for `NEP-245`/`IMT` token ids that can represent variable, semi-fungible quantities.

## Proof of Concept
`cargo test` in `contracts/defuse/core` (unit-level, exercising `TokenDiff::execute_intent`/`Engine`):
1. Set `protocol_fee = Pips::ONE_PERCENT`.
2. Create a high-value NEP-245 token `T` and a NEP-141 token `U`.
3. Case A (baseline, single intent): signer A signs one `TokenDiff{T: -100, U: +closure}`, signer B signs the matching counter-intent; execute via `execute_signed_intents`; assert `fees_collected(T) == Self::token_fee(T, 100, fee).fee_ceil(100) > 0`.
4. Case B (exploit): signer A signs 100 separate `TokenDiff{T: -1, U: +u_i}` intents (distinct nonces), signer B signs 100 matching counter-intents; execute all 100 pairs in one `execute_signed_intents([...])` call; assert:
   - `fees_collected(T)` summed across all 100 intents `== 0`.
   - Aggregate `T` moved from A to B `== 100` (same as Case A).
   - Aggregate `U` moved `== ` value equivalent to Case A's `U` leg, still incurring the full `Pips::ONE_PERCENT` fee on `U` alone.
5. Compare Case A vs Case B: identical net value transferred for `T`, but `fees_collected(T)` differs (`>0` vs `0`), demonstrating the fee bypass on the NEP-245 leg purely via intent splitting.

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
