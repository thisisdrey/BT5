### Title
Protocol fee bypass on Nep245/Imt token diffs by splitting a large transfer into many `|delta|==1` `TokenDiff` intents - (`contracts/defuse/core/src/intents/token_diff.rs:206-216`)

### Finding Description
`TokenDiff::execute_intent` computes fees per intent, per token leg, using the raw `amount = delta.unsigned_abs()` of that single leg: `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` [1](#0-0) . `token_fee` special-cases `TokenIdType::Nep245`/`Imt`: fee is only charged `if amount > 1`; otherwise it returns `Pips::ZERO` unconditionally, regardless of `protocol_fee` [2](#0-1) . This rule was written to avoid charging proportional fees on genuinely indivisible NFT-like MT items (comment: "do not take fees on NFTs and MTs with `|delta| <= 1`"), but `Nep245`/`Imt` token ids can also represent ordinary fungible balances of arbitrary size, and the code has no way to distinguish an indivisible unit from a fungible balance being deliberately chunked.

The broken binding: `sum(fees_collected across all TokenDiff intents in the batch for token T)` should equal `Pips::fee_ceil` applied to the aggregate negative delta of `T` across the batch, i.e. `fee_ceil(N)` for a signer moving `N` units of `T`. Because `execute_signed_intents` iterates each `MultiPayload`/intent independently via `Engine::execute_signed_intent` → `intents.execute_intent(...)` [3](#0-2) , and `token_fee` is evaluated per-leg with `amount=1` each time, a signer who submits `N` separate `TokenDiff` intents (each `{T: -1}`) inside one batch pays zero fee in total, while a signer submitting a single `TokenDiff` intent `{T: -N}` (same aggregate value moved) pays `fee_ceil(N) = protocol_fee * N` (rounded up). No existing guard (`MultiPayload::verify`, nonce/salt checks, `TransferMatcher::finalize`, balance invariant checks) aggregates deltas across intents or across a batch for fee purposes — each intent's fee is computed in isolation and immediately credited via `internal_add_balance` to `fee_collector` [4](#0-3) .

Exploit: attacker holds an Nep245 (or Imt) balance of `N` fungible units inside the Verifier. They sign one `MultiPayload` containing `N` `TokenDiff` intents, each `diff = {T: -1, other_token: +k}` (or any valid closing pair), instead of a single `{T: -N, other_token: +N*k}`. Each leg hits the `amount > 1` false branch and returns `Pips::ZERO`, so `fees_collected` is `0` for every leg, and the batch collects zero protocol fee on `N` units moved instead of `fee_ceil(N)`.

### Impact Explanation
This directly bypasses the protocol fee mechanism for any fungible balance represented via `TokenIdType::Nep245` or `TokenIdType::Imt`, which under the current fee model should always attract `protocol_fee` on `token_in` legs when `protocol_fee` is nonzero. `fee_collector` under-collects fees on every trade routed through these token types when the signer chunks the trade into unit legs, and this is repeatable for every account and every Nep245/Imt token, with no upper bound other than the number of intents that fit in gas/a `MultiPayload`. This matches the "protocol fees bypassed" Critical category.

### Likelihood Explanation
No special privileges are required — any signer with an Nep245/Imt balance inside the Verifier can construct a `MultiPayload` with many `TokenDiff` intents of `|delta| == 1` and a matching counter-leg to keep the diff closed (e.g., paired with a counterparty's opposite `TokenDiff`, or via `closure`). The cost is only the size/gas of the batch (linear in `N`), which is a normal transaction cost, not a barrier. It is trivially repeatable across accounts, tokens, and batches.

### Recommendation
Compute and check fee eligibility based on aggregated token deltas rather than a single leg's raw amount — e.g., aggregate all `TokenDiff` intents for the same signer/token within a batch (or across the whole `Deltas`/`Transfers` accumulation) before deciding whether the Nep245/Imt `amount > 1` fee exemption applies, or remove the per-leg `amount <= 1` fee exemption for `Nep245`/`Imt` token types entirely and only apply it to genuinely non-fungible `Nep171` tokens.

### Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (or a workspace-level near-workspaces test)
// Setup: signer has Nep245 balance of N=100 units of `mt:token`, protocol_fee = 1% (Pips::ONE_PERCENT).
//
// Test A (single intent): sign one MultiPayload with one TokenDiff intent:
//   diff = { mt_token: -100, ft_out: +100_000 }
// After execute_signed_intents: assert fees_collected for mt_token == Pips::ONE_PERCENT.fee_ceil(100) > 0

// Test B (split intents): sign one MultiPayload with 100 TokenDiff intents, each:
//   diff_i = { mt_token: -1, ft_out: +1_000 }
// After execute_signed_intents: assert total fees_collected for mt_token across all 100 intents == 0

// Binding under test:
//   sum(fees_collected[mt_token] over Test B's 100 intents) == fee_ceil(100) [same as Test A]
// Actual: Test B side == 0, Test A side > 0 → binding violated, fee bypass confirmed.
```

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-78)
```rust
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
