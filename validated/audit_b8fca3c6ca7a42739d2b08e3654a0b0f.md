### Title
Fee bypass on `TokenId::Nep245`/`Imt` diffs via same-signer delta splitting across multiple `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives the protocol fee on `TokenId::Nep245`/`Imt` when `amount <= 1`, and this threshold is evaluated per-intent inside `TokenDiff::execute_intent`, independently for each signed `TokenDiff`. A signer can split a single `delta = -N` transfer into `N` separate signed `TokenDiff` intents each with `delta = -1` on the same `Nep245TokenId`, executed together in one `execute_signed_intents` batch, so that every individual fee computation sees `amount = 1` and charges zero fee, while `internal_apply_deltas`/`internal_sub_balance` cumulatively debit the full `N` units from the signer.

### Finding Description
The broken binding: total `fees_collected` credited to `fee_collector` for `TokenId` across the split intents should equal `Pips::fee_ceil` applied once to the combined `amount = N`, but instead equals `0` for any `N` when chunked into unit legs.

Code path: [1](#0-0)  computes, for each `(token_id, delta)` pair inside a *single* `TokenDiff.diff`, `amount = delta.unsigned_abs()` and `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, then adds it to a per-intent `fees_collected` that is credited to `fee_collector` at the end of that one `execute_intent` call. `token_fee` explicitly zeroes the fee for `Nep245`/`Imt` when `amount <= 1`: [2](#0-1) .

Each `TokenDiff` intent is executed independently by `Engine::execute_signed_intent`, once per signed payload in the batch, with its own `nonce`/`intent_hash`: [3](#0-2) . The signer fully controls their own nonces and can sign as many `TokenDiff` payloads as they like with no lower bound on delta size or upper bound on intent count per batch.

Because the fee-zero threshold (`amount <= 1`) is checked against the amount of a single intent's delta rather than the signer's net position on that `TokenId` across the whole batch, a signer trading `delta = -N` on a `Nep245`/`Imt` token can always split it into `N` intents of `delta = -1` each. Every such intent independently satisfies `amount <= 1`, so `token_fee` returns `Pips::ZERO` and no fee is added to `fees_collected` for any of them. `internal_apply_deltas` (via `internal_sub_balance`) still debits the full `N` units from the signer's balance across the `N` intents, and the batch-wide `TransferMatcher::finalize` in `contracts/defuse/core/src/engine/state/deltas.rs` only checks that net deltas across the whole batch sum to zero per `TokenId` — it performs no fee-related check and does not re-aggregate per-signer amounts for fee purposes. Thus the invariant/matching logic is orthogonal to, and does not compensate for, the fee bypass.

Attacker's payload: two (or `N`) `MultiPayload` signed by the same signer key, each containing one `DefuseIntents` with a single `TokenDiff { diff: { Nep245TokenId(X): -1, other_token: +y_i } }`, distinct nonces, submitted together to `execute_intents`. A counterparty (e.g. a solver) supplies the matching positive/negative legs as usual; the only change from a normal trade is that the signer's leg on the `Nep245` token is chunked into unit-size intents instead of one intent with `delta = -N`.

No existing guard prevents this: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize`'s zero-sum check all operate on signature validity and net-balance conservation, not on fee aggregation across intents. `token_fee`'s `amount <= 1` rule is evaluated strictly locally to the current intent's delta map.

### Impact Explanation
The protocol permanently under-collects the fee that would otherwise be credited to `fee_collector` on any `Nep245`/`Imt`-denominated trade, for any principal amount, at zero cost beyond ordinary signing/gas overhead for extra intents. This is a repeatable, scalable fee bypass (protocol revenue loss) reachable by any unprivileged signer trading their own balance, matching the "protocol fees bypassed" Critical impact category. It does not, however, let an attacker move funds without authorization or break the Verifier's invariant — the shortfall is specifically the fee that `fee_collector` would have received, not funds stolen from other users' balances.

### Likelihood Explanation
Trivially exploitable: any account holding `Nep245`/`Imt` balance in Defuse can chunk its own outgoing legs into `delta = -1` intents, each independently signed with the account's own key and any unused nonce, and submit them in the same `execute_intents`/`simulate_intents` batch as the rest of a normal trade. No special role, balance, or counterparty cooperation beyond what a normal swap already requires is needed. The only added cost is linear in the number of chunks (extra signature/payload/gas overhead), which is negligible relative to the fee saved for typical protocol fee rates.

### Recommendation
Aggregate the `amount <= 1` fee-exemption threshold check against the signer's net delta per `TokenId` across the whole `execute_signed_intents` batch (or across all `TokenDiff` intents signed within it), rather than per individual intent, before deciding whether to zero the fee — e.g., accumulate per-signer/per-token deltas via `Deltas`/`TransferMatcher` before computing `token_fee`, or compute fees once against total absolute delta per token per signer at finalize time.

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Set up `env` with a nonzero `Pips` fee such that `fee_ceil(2) >= 1` but `fee_ceil(1) == 0` for a `Nep245TokenId`.
2. Sign one `TokenDiff` from `user1` with `diff = { nep245_token: -2, ft_out: +Y }` (single intent, single nonce) matched by a counterparty intent; execute and record `fees_collected` for `nep245_token` — expect it equals `Pips::fee_ceil` on `amount=2`.
3. Reset state; sign two separate `TokenDiff` payloads from the same `user1` account with different nonces: `diff1 = { nep245_token: -1, ft_out: +y1 }`, `diff2 = { nep245_token: -1, ft_out: +y2 }` where `y1+y2 = Y`, matched by counterparty intent(s) providing `+2 nep245_token / -Y ft_out`.
4. Execute both signed payloads together via one `env.defuse_execute_intents` / `execute_signed_intents` call.
5. Assert: total `fees_collected` for `nep245_token` summed across both intents (via `TokenDiffEvent::fees_collected` or `fee_collector`'s balance delta) is `0`, while step 2's single-intent fee was `> 0` — demonstrating the binding `total_fee(split) == fee_ceil(combined amount=2)` is violated (`0 != fee_ceil(2)`).

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
