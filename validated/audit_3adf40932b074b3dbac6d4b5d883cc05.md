### Title
Per-intent fee exemption for NEP-245/IMT amounts ≤1 enables leg-splitting to bypass protocol fees entirely - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` rejects a per-token `delta == 0` but allows `delta == ±1`, and `TokenDiff::token_fee` explicitly returns `Pips::ZERO` for `Nep245`/`Imt` token ids whenever `amount <= 1`, while `Nep141` always uses the real `fee` regardless of amount. Because the fee is computed independently per `execute_intent` call on the *per-intent* delta rather than on any cumulative volume, an attacker can split a large NEP-245/IMT trade into many `delta = -1` legs and pay zero fee on the entire aggregate volume, whereas performing the same aggregate volume in a single intent (or via NEP-141) would incur `fee.fee_ceil(amount)`.

### Finding Description
The relevant code is in `contracts/defuse/core/src/intents/token_diff.rs`, not `nonce/mod.rs` as stated in the question (no such fee logic exists in the nonce module) [1](#0-0) .

Binding claimed vs. actual: `fee_collected(T) == Pips::fee_ceil(protocol_fee, |Σ negative deltas of T over the whole trading session|)` should hold independent of how the negative deltas are split across intents. In the actual code, fee is computed strictly per-`TokenDiff` intent, per-token, using only that intent's local `amount = delta.unsigned_abs()`:

```
for (token_id, delta) in &self.diff {
    if *delta == 0 { return Err(DefuseError::InvalidIntent); }
    ...
    if *delta < 0 {
        let amount = delta.unsigned_abs();
        let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
        ...
    }
}
``` [1](#0-0) 

`token_fee` special-cases `Nep245`/`Imt` (and `Nep171`) to return `Pips::ZERO` whenever `amount <= 1`:
```
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
``` [2](#0-1) 

`Pips::fee_ceil` for a `Nep141` token with `amount == 1` and any nonzero `fee` always rounds up to at least 1 unit (`checked_mul_div_ceil`), so `Nep141` never gets the exemption [3](#0-2) .

Exploit flow: the attacker (as both maker and taker, or with a colluding/self-controlled counterparty) constructs `N` separate signed `TokenDiff` intents (distinct nonces, batched in one `execute_intents(Vec<MultiPayload>)` call or across calls), each moving `delta = -1` of the same NEP-245/IMT `token_id`. Each intent independently computes `amount = 1`, so `token_fee` returns `Pips::ZERO` and `fee_ceil(1) == 0` for every leg. Summed across `N` legs, cumulative fee collected is `0`, whereas a single `TokenDiff` intent moving `delta = -N` (`N > 1`) on the same token would trigger `fee.fee_ceil(N) > 0` for any nonzero `protocol_fee`. `TransferMatcher::finalize` nets these deltas across all executed intents into the same aggregate `Transfers` regardless of how many intents produced them [4](#0-3) , so the on-chain economic outcome is identical to the single large trade — only the fee differs.

No existing guard prevents this: `execute_intent`'s zero-delta check only rejects `delta == 0`, not `delta == ±1` [5](#0-4) ; nonce/signature verification in `execute_signed_intent` has no notion of cumulative volume across intents/nonces [6](#0-5) ; and there is no cross-intent fee aggregation anywhere in the engine.

### Impact Explanation
The `fee_collector` account is systematically under-credited for NEP-245/IMT trading volume that is structured as unit-sized (`|delta|==1`) legs, while economically identical volume moved through a single larger intent (or via NEP-141) pays the full protocol fee. This falls under "protocol fees bypassed" — a defined Critical impact category. The blast radius covers any deployment where the `imt`/NEP-245 feature is enabled and `protocol_fee > 0`; it is fully repeatable by any signer, requires no privileged role, and scales linearly with the number of split legs the attacker is willing to submit (only bounded by gas/transaction size, which is explicitly out of scope for disqualification).

### Likelihood Explanation
The attacker needs only their own signing keys and control of both sides of a trade (or a willing/self-controlled counterparty), a deployed or existing NEP-245/IMT token, and the ability to sign multiple `TokenDiff` intents with distinct nonces — all within the stated unprivileged-attacker capabilities. Cost is proportional to the number of split legs (gas + relayer fees), which the rules explicitly exclude from scope as a disqualifier ("unbounded gas or storage consumption... out of scope" governs DoS claims, not this fee-bypass claim). The mechanism is deterministic and requires no race conditions or timing — every unit-sized NEP-245/IMT negative delta always yields `Pips::ZERO` fee under the current code.

### Recommendation
Compute the fee exemption based on the token's actual divisibility/nature (e.g., a stored "non-fungible" flag or NEP-245 metadata) rather than purely on the numeric `amount` of a single intent's delta. Alternatively, track and aggregate NEP-245/IMT fee-taking on a cumulative, per-account/per-session basis (or at least require `fee_ceil` to apply once cumulative absolute volume for that `token_id`/signer within a batch exceeds 1), so that splitting a trade into many unit legs cannot zero out the fee that a single equivalent-volume intent would incur.

### Proof of Concept
`cargo test` (in `contracts/defuse/core` or `tests/`), sandbox/unit-style, comparing total fee collected for equal aggregate volume:
1. Set `protocol_fee = Pips::ONE_PERCENT` (or any nonzero value).
2. Deploy/register one NEP-245 token id `T` and one NEP-141 token id `F`, deposit balances for a maker/taker pair (attacker-controlled both).
3. Scenario A (single intent): submit one `TokenDiff` intent with `diff = {T: -1000, out_token: +X}` and record `fees_collected` via the `DefuseEvent::TokenDiff` event / `Amounts` returned; assert `fee_A = protocol_fee.fee_ceil(1000) > 0`.
4. Scenario B (leg-split): submit 1000 separate signed `TokenDiff` intents, each `diff = {T: -1, out_token: +x}` (`x = X/1000`), batched via `execute_signed_intents`; sum `fees_collected` across all 1000 events; assert `fee_B == 0`.
5. Assert `fee_A != fee_B` (specifically `fee_A > 0 == fee_B`) despite identical aggregate `T` volume moved (`1000` units in both cases), demonstrating the fee bypass.
6. Repeat with `T` as a `Nep141` token instead of `Nep245`/`Imt` to show `fee_ceil(1)` per leg is nonzero for NEP-141 (`fee_B_nep141 > 0`), confirming the token-type-dependent asymmetry described in `token_fee` [2](#0-1) .

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

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
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
