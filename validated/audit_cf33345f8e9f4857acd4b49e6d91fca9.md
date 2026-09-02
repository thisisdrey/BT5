### Title
Fee bypass on Nep245/Imt `TokenDiff` legs via splitting a large delta into unit-amount legs - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` (contracts/defuse/core/src/intents/token_diff.rs:206-217) returns `Pips::ZERO` for `TokenIdType::Nep245`/`Imt` whenever the per-intent `amount <= 1`, and `TokenDiff::execute_intent` computes the fee independently for each `TokenDiff` intent processed [1](#0-0) . Because `DefuseIntents.intents` is an unconstrained `Vec<Intent>` [2](#0-1) , a signer can submit N separate `TokenDiff` intents each with `delta = -1` on the same `TokenId` in one `MultiPayload`, instead of a single `TokenDiff` with `delta = -N`, so `fees_collected` sums to `0` instead of `Pips::fee_ceil(fee, N)`.

### Finding Description
The binding claimed to hold is: `fees_collected(T) == Pips::fee_ceil(fee, sum(|delta<0| for T))` for token `T` over a signer's execution. This binding is broken because the fee is computed **per intent**, not on the aggregate negative delta for a token across the whole execution.

`TokenDiff::execute_intent` loops over `self.diff` (a `BTreeMap<TokenId, i128>`, which by construction can only have one entry per `TokenId` *within a single intent*) and, for negative deltas, computes `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` using only that single intent's `amount` [3](#0-2) . `token_fee` explicitly special-cases `TokenIdType::Nep245 | TokenIdType::Imt` to return `Pips::ZERO` when `amount <= 1` (comment: "do not take fees on NFTs and MTs with |delta| <= 1") [4](#0-3) .

Because `DefuseIntents::intents` is `Vec<Intent>` with no deduplication or aggregation across intents for the same `TokenId` [2](#0-1) , an unprivileged signer can put N `TokenDiff` intents in one signed `DefuseIntents` message, each with `diff = {T: -1, ...}` instead of one `TokenDiff` with `diff = {T: -N, ...}`. `Engine::execute_signed_intent` verifies the signature/nonce once for the whole batch and then calls `intents.execute_intent(...)`, which dispatches each `Intent::TokenDiff` to `TokenDiff::execute_intent` independently [5](#0-4) . Each of the N calls sees `amount == 1` and returns fee `0`, so total `fees_collected` for token `T` is `0`, whereas a single intent moving `-N` at once would have applied `fee_ceil(fee, N) > 0`.

The overall balance-matching invariant enforced by `TransferMatcher::finalize` (contracts/defuse/core/src/engine/state/deltas.rs:267-283) still requires that withdrawals for `T` across the whole batch be matched by deposits from counterparties, so this does not let value leave the Verifier's custody unmatched — but it does let the signer avoid paying the protocol fee that would otherwise be charged on the aggregate amount moved, since fee computation is not derived from, or checked against, the aggregate per-token delta.

No existing guard (`MultiPayload::verify`, `verify_intent_nonce`, `MaybeLegacyNonces::commit`, `Lock::get_mut`, `TransferMatcher::finalize`, `assert_one_yocto`, role/pause guards) inspects or aggregates per-token deltas across intents before calling `token_fee`, so nothing prevents this divergence.

### Impact Explanation
This directly falls under "protocol fees bypassed or over-collected," listed as a Critical impact category. The fee collector (`ContractState.fees: FeesConfig`, wired at contracts/defuse/src/contract/state/mod.rs) receives `0` fee on any Nep245/Imt-classified fungible-style token trade that a signer chooses to structure as N unit-amount `TokenDiff` intents in a single signed payload, regardless of how large the aggregate amount moved is. This is repeatable by any signer, on any Nep245/Imt token, for every batch they submit, at the cost of only larger payload size (more intents in the `Vec`). The blast radius is protocol-wide fee revenue on that token class; it does not, by itself, permit theft of the Verifier's custodied funds (the `TransferMatcher` netting invariant is unaffected), but it does allow the fee due on token `T`'s aggregate delta to be silently understated to zero versus what an equivalent single-intent trade of the same size would pay.

### Likelihood Explanation
Preconditions: `ContractState.fees.fee > Pips::ZERO`, an existing balance of a token classified `TokenIdType::Nep245`/`Imt`, and a counterparty (or matching legs) supplying the equal-and-opposite deposits so `TransferMatcher::finalize` succeeds — exactly the same counterparty requirement as the equivalent single large-delta trade would need. The attacker cost is only constructing and signing one `DefusePayload` containing N `TokenDiff` intents instead of one, well within reach of any unprivileged signer able to call `execute_intents`/`simulate_intents`. This is trivially and fully repeatable.

### Recommendation
Compute `token_fee` from the aggregate absolute negative delta per `TokenId` across the entire `DefuseIntents`/execution (or across the whole `Deltas`/`TransferMatcher` accumulation) rather than per individual `TokenDiff` intent, so that splitting one logical trade into many `TokenDiff` intents cannot change the total fee charged. Alternatively, restrict the Nep245/Imt `amount <= 1` fee exemption to be evaluated against the total balance delta for that `TokenId` and `signer_id` within the whole batch, not the per-intent delta.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (unit test, no sandbox needed):
1. Build an `Engine` with `protocol_fee = Pips::ONE_PERCENT` (or any non-zero fee) and a mock `State`/`Inspector` giving the signer a sufficient balance of a `Nep245`/`Imt` `TokenId` `T`.
2. Case A ("single big leg"): execute one `TokenDiff { diff: {T: -100, T2: +X} }` intent; record `fees_collected` for `T` from the emitted `TokenDiffEvent`/return path — expect `Pips::ONE_PERCENT.fee_ceil(100) > 0`.
3. Case B ("100 unit legs"): execute 100 separate `TokenDiff { diff: {T: -1, T2: +x_i} }` intents (summing to the same aggregate `-100`/`+X`) for the same `signer_id` within one `execute_signed_intents` batch/`Engine`; sum `fees_collected` for `T` across all 100 intents — expect `0`.
4. Assert `fees_collected_case_A(T) != fees_collected_case_B(T)`, specifically `fees_collected_case_A(T) > 0 == fees_collected_case_B(T)`, proving the `FEES` equality `fees_collected(T) == Pips::fee_ceil(fee, sum(|delta<0|))` is violated for Case B.

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

**File:** contracts/defuse/core/src/intents/mod.rs (L30-37)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
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
