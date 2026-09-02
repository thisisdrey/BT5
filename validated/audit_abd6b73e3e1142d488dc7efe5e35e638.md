### Title
Protocol fee bypass on `Nep245`/`Imt` tokens via unit-sized `TokenDiff` splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` unconditionally returns `Pips::ZERO` whenever the per-intent `|delta|` for a `Nep245`/`Imt`/`Nep171` token is `<= 1`, regardless of the DAO-configured `protocol_fee`. Because the fee is computed independently per `TokenDiff` intent (not aggregated across the whole `MultiPayload`/batch), an attacker can split any large `Nep245`/`Imt` volume into many unit legs (`delta = ±1`) across multiple `TokenDiff` intents in one signed payload (or across many transactions) and pay zero fee on the whole transferred amount, whereas a single intent moving the same total amount in one `delta` would pay `Pips::fee_ceil(amount) > 0`.

### Finding Description
Binding claimed to hold: `fees_credited_to_fee_collector(T) == Pips::fee_ceil(total_negative_delta_of_T_moved_in_call)`.

The actual code in `contracts/defuse/core/src/intents/token_diff.rs`:
```
59: for (token_id, delta) in &self.diff {
...
70:     if *delta < 0 {
71:         let amount = delta.unsigned_abs();
72:         let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
and
```
206: pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
207:     let token_id = token_id.into();
208:     match token_id {
209:         TokenIdType::Nep141 => {}
210:         TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
211:         // do not take fees on NFTs and MTs with |delta| <= 1
212:         TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
213:     }
214:     fee
215: }
```
`amount` here is the magnitude of a single `TokenDiff` intent's delta for that token, not the aggregate volume moved by the signer/batch for that `token_id`. `DefuseIntents::execute_intent` iterates over each `Intent` (including each `TokenDiff`) independently [1](#0-0) , so each `TokenDiff` intent's fee is computed in isolation using only its own local `delta`.

Exploit: the attacker (and/or a colluding counterparty they control) signs a `MultiPayload` containing `K` separate `TokenDiff` intents, each with a single-entry diff `{token_id: -1}` (paired with matching `+1` legs elsewhere in the batch to satisfy the zero-sum invariant enforced at `finalize`/`TransferMatcher`). Each leg has `amount == 1`, so `token_fee` returns `Pips::ZERO` for every leg regardless of `protocol_fee`, and `fees_collected` for that token stays `0` no matter how large `K` is. An equivalent single `TokenDiff` moving `K` units in one `delta` (`amount = K > 1`) falls into the `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1` branch and pays the full `fee.fee_ceil(K) > 0`.

No existing guard prevents this: `self.diff.is_empty()` and `*delta == 0` checks (lines 52-54, 60-62) don't restrict the number of intents or aggregate volume; nonce/signature verification in `Engine::execute_signed_intent` [2](#0-1)  only ensures the whole `MultiPayload` is validly signed once — it does not limit or aggregate per-token fee-eligible amounts across the multiple `TokenDiff` intents it authorizes.

### Impact Explanation
The fee_collector's expected protocol fee revenue on `Nep245`/`Imt` (multi-token/semi-fungible) token movements can be driven to zero for arbitrarily large volumes, by construction, at the attacker's discretion — this directly matches the "protocol fees bypassed" Critical category. This is repeatable indefinitely across accounts, tokens (any `Nep245`/`Imt` token_id), and batches; the attacker only needs to be able to sign `TokenDiff` intents (either self-paired swaps or coordinated with a second attacker-controlled account) and pay ordinary NEAR gas/tx costs.

### Likelihood Explanation
No special privilege is required — any unprivileged signer can construct such a `MultiPayload`. The only practical constraint is per-transaction gas/payload size limits (explicitly out of scope for DoS but not relevant to blocking this attack, since the attacker can simply issue multiple `execute_intents` calls to move volume in chunks over time). Both accounts can be controlled by the same attacker (self-trading) so no cooperation from a third party is required. Feasibility is high and cost is low (dominated by ordinary gas costs of many small intents).

### Recommendation
Do not exempt `Nep245`/`Imt` fee based on a single intent's local `|delta| <= 1`; either (a) always charge `protocol_fee` on `Nep245`/`Imt` negative deltas regardless of magnitude, tracking fee-exemption per distinct sub-token only when it is verifiably non-fungible (e.g., via token metadata/supply, not just per-leg amount), or (b) aggregate the fee computation per `token_id` across all `TokenDiff` intents belonging to the same signer (or same `MultiPayload`/batch) before applying the `amount > 1` threshold, so that splitting one logical transfer into many unit legs cannot change the total fee charged.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or tests/ crate) — pseudocode outline
// 1. Set protocol_fee = Pips::ONE_PERCENT (>0), Nep245 token T, K = 1000.
// 2. Build MultiPayload A with K TokenDiff intents, each {T: -1}, signed by attacker.
//    Build matching MultiPayload B with K TokenDiff intents, each {T: +1}, signed by counterparty
//    (or attacker's second account), so per-token net delta sums to 0 across the batch.
// 3. Call engine.execute_signed_intents([...A, ...B]) (or execute_intents equivalent).
// 4. Assert: fee_collector's balance for T == 0 after execution (binding LHS).
// 5. In a control test, sign ONE TokenDiff intent with delta = -K for the same token T
//    (paired with a +K counter-leg), execute it, and assert fee_collector's balance for T
//    == Pips::ONE_PERCENT.fee_ceil(K) > 0 (binding RHS).
// 6. Show LHS (0) != RHS (fee_ceil(K)) for identical total volume K, for all K > 1,
//    confirming the fee converges to 0% as the attacker increases K via unit-splitting.
```

### Citations

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
