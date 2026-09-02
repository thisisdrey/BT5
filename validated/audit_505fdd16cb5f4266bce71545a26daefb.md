### Title
Fee bypass on Nep245/Imt `TokenDiff` intents via splitting a single-token delta across multiple intent objects in one batch - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::execute_intent` computes `token_fee` and `Pips::fee_ceil` per individual `TokenDiff` intent object, using only that object's own `delta` magnitude, rather than the aggregate negative delta for a given `token_id` across the whole signed batch. Because `TokenDiff::token_fee` explicitly returns `Pips::ZERO` for `Nep245`/`Imt` tokens whenever the per-object `amount <= 1`, a signer can split one legitimate `-k` delta on a Nep245/Imt token into `k` separate `TokenDiff` intent objects each with `delta = -1`, causing the protocol fee to be computed as zero `k` times instead of `fee.fee_ceil(k)` once.

### Finding Description
The broken binding: expected `total_fee_for_token_X = fee.fee_ceil(sum_of_negative_deltas_for_token_X_in_batch)`, but the code produces `total_fee_for_token_X = Σ_i fee.fee_ceil_per_object(|delta_i|)` where each `delta_i` is the delta declared in one `TokenDiff` intent object.

In `TokenDiff::execute_intent` [1](#0-0) , the fee is computed once per call to `execute_intent`, iterating only over `self.diff` (the deltas contained in *that one* `TokenDiff` object). `TokenDiff::token_fee` explicitly special-cases `Nep245`/`Imt` tokens: [2](#0-1) 

For `Nep245`/`Imt`, if `amount <= 1`, `Pips::ZERO` is returned unconditionally, regardless of the configured protocol `fee`. This exemption was evidently intended for genuine one-off NFT-style transfers, but nothing prevents an attacker from representing a legitimate `k`-unit Nep245 trade as `k` distinct `TokenDiff` intent objects (each `delta = -1`, potentially each with a distinct `memo`/`referral`), all signed under one `DefuseIntents` message (or split across multiple signed payloads within the same `execute_intents`/`simulate_intents` batch).

`Intent::execute_intent` dispatches per-intent (no aggregation across intents of the same type) [3](#0-2) , and `Engine::execute_signed_intent` simply calls `intents.execute_intent(...)` once per signed payload and defers only balance netting (not fee computation) to the final `TransferMatcher::finalize()` [4](#0-3) . The `TransferMatcher`/`Deltas` machinery in `engine/state/deltas.rs` only nets raw token amounts across the whole batch to compute `Transfers`; it never re-derives or re-checks fees against the netted amount [5](#0-4) . Thus balance-invariant checking (which correctly requires the sum of all deltas to net to zero, and does not distinguish per-object fee accounting) provides no protection against this fee-computation-granularity mismatch.

Attacker exact payload: sign one `DefusePayload<DefuseIntents>` containing `k` `TokenDiff` intents, each `{ diff: {Nep245Token: -1}, memo: Some("distinct"), referral: Some(attacker_or_any) }`, matched (within the same `execute_intents`/`simulate_intents` batch, e.g., by a counterparty's own signed payload) by a total `+k` credit on the same token elsewhere in the batch. Each of the `k` `execute_intent` calls independently computes `amount = 1`, `Self::token_fee(Nep245, 1, fee) == Pips::ZERO`, so `fees_collected` stays empty for every one of the `k` objects, and `fee_collector` never receives `fee.fee_ceil(k)` that would have applied had the same net trade been expressed as a single `TokenDiff` with `delta = -k`.

None of `MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, nonce commitment, or `TransferMatcher::finalize` prevent this, because they only validate signature/replay/netting — none re-derives fees from netted per-token amounts across the batch.

### Impact Explanation
This is a protocol-fee-under-collection issue against `fee_collector`'s expected revenue, matching the explicitly listed Critical category "protocol fees bypassed or over-collected." It is repeatable indefinitely by any signer trading Nep245/Imt tokens: any trade volume `k` on a Nep245/Imt token can be fully de-fee'd by splitting into `k` unit-delta `TokenDiff` objects in the same signed batch, at the cost of slightly larger payload size (more intent objects to sign/serialize), which is negligible. This only affects fees on Nep245 (multi-token)/Imt token diffs where `amount > 1` would otherwise apply a fee — it does not affect Nep141 (fungible token) diffs, since `token_fee` always applies `fee` for `Nep141` regardless of amount [6](#0-5) . Balance correctness/invariants are still fully preserved (no funds are stolen from other users, no double-settlement); the only leakage is the protocol's own fee revenue.

### Likelihood Explanation
No special privileges, roles, or balances beyond normal trading are required — any unprivileged signer who trades Nep245/Imt-typed tokens through `TokenDiff` intents can trivially restructure a single intent into many unit-delta intents within the same signed message before sending it via `execute_intents`/`simulate_intents`. The cost is only marginal extra payload/signature size; there is no cap preventing many `TokenDiff` objects with `memo`/`referral` fields in one `DefuseIntents` batch found in the reviewed code. This is straightforward and fully repeatable for every Nep245/Imt trade with `amount > 1`.

### Recommendation
Aggregate negative deltas per `token_id` across all `TokenDiff` intents within the same execution scope (at minimum within one `DefuseIntents` batch, ideally within the whole `execute_signed_intents` call) before applying `TokenDiff::token_fee`/`Pips::fee_ceil`, rather than computing fees independently per intent object. Alternatively, remove/restrict the Nep245/Imt "amount <= 1 → zero fee" exemption to contexts where the token is verified to be non-fungible (e.g., NFT-typed Nep245), or perform fee assessment centrally in `Engine`/`Deltas` against the final netted `TransferMatcher` amounts rather than in each `TokenDiff::execute_intent` call.

### Proof of Concept
```rust
// tests/src/tests/defuse/intents/token_diff.rs (new test, near-workspaces sandbox)
// 1. Deploy Env with fee = Pips::ONE_PERCENT (or any non-zero fee).
// 2. Mint a Nep245 (multi-token) balance of `k` units of one token_id to `attacker`,
//    and mint a matching Nep141 (or Nep245) balance to `counterparty` that will be
//    exchanged for the attacker's k units.
// 3. Attacker signs ONE DefuseIntents payload containing k TokenDiff intents:
//    for i in 0..k {
//        TokenDiff { diff: {mt_token_id.clone(): -1}, memo: Some(format!("leg-{i}")), referral: None }
//    }
//    (each intent individually valid: diff non-empty, delta != 0)
// 4. Counterparty signs a TokenDiff crediting attacker +k of some other token, and
//    debiting itself by the equivalent (or debiting -k mt_token_id credit elsewhere to net to zero).
// 5. Call env.defuse.execute_intents(MultiPayloadArgs { signed: &[attacker_payload, counterparty_payload] }).
// 6. Assert:
//    let expected_fee = Pips::ONE_PERCENT.fee_ceil(k); // > 0 for k large enough
//    let actual_fee_collector_mt_balance = mt_balance_of(fee_collector, mt_token_id);
//    assert!(actual_fee_collector_mt_balance < expected_fee);
//    // e.g. assert_eq!(actual_fee_collector_mt_balance, 0) while expected_fee > 0
//
// Compare against a control test where the same net -k delta is submitted as a
// SINGLE TokenDiff intent (delta = -k), asserting fee_collector's mt balance
// equals Pips::ONE_PERCENT.fee_ceil(k) exactly — demonstrating the discrepancy
// between the split-batch case (fee = 0) and the single-intent case (fee > 0)
// for the same net token movement.
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

**File:** contracts/defuse/core/src/intents/mod.rs (L115-145)
```rust
impl ExecutableIntent for Intent {
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
        match self {
            Self::AddPublicKey(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::RemovePublicKey(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::Transfer(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::FtWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::NftWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::MtWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::NativeWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::StorageDeposit(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::TokenDiff(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::SetAuthByPredecessorId(intent) => {
                intent.execute_intent(signer_id, engine, intent_hash)
            }
            Self::AuthCall(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            #[cfg(feature = "imt")]
            Self::ImtMint(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            #[cfg(feature = "imt")]
            Self::ImtBurn(intent) => intent.execute_intent(signer_id, engine, intent_hash),
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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L233-284)
```rust
/// Accumulates internal deposits and withdrawals on different tokens
/// to match transfers using `.finalize()`
///
/// Transfers in `TokenDiff` intents are represented as deltas without receivers.
/// This struct accumulates tokens all transfers, and converts them from deltas, to
/// a set of transfers from one account to another.
/// Note that this doesn't touch account balances. The balances were already changed
/// in an earlier stage while executing the intent.
#[derive(Debug, Default)]
pub struct TransferMatcher(HashMap<TokenId, TokenTransferMatcher>);

impl TransferMatcher {
    #[inline]
    pub fn new() -> Self {
        Self(HashMap::new())
    }

    #[inline]
    pub fn deposit(&mut self, owner_id: AccountId, token_id: TokenId, amount: u128) -> bool {
        self.0.entry_or_default(token_id).deposit(owner_id, amount)
    }

    #[inline]
    pub fn withdraw(&mut self, owner_id: AccountId, token_id: TokenId, amount: u128) -> bool {
        self.0.entry_or_default(token_id).withdraw(owner_id, amount)
    }

    #[inline]
    pub fn add_delta(&mut self, owner_id: AccountId, token_id: TokenId, delta: i128) -> bool {
        self.0.entry_or_default(token_id).add_delta(owner_id, delta)
    }

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
