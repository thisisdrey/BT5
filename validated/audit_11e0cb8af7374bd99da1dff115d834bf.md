### Title
Protocol fees bypassed on Nep245/Imt tokens by splitting a `delta=-N` transfer into N separate `delta=-1` `TokenDiff` intents - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` tokens only when the per-intent `amount <= 1`, and `TokenDiff::execute_intent` computes and collects that fee independently for each signed `TokenDiff` intent using only that intent's own `delta`. Because a signer can submit any number of separately-signed `TokenDiff` intents in one `execute_intents`/`execute_signed_intents` batch, a transfer of `N` units on a Nep245/Imt token can be split into `N` intents each moving `delta = -1`, each individually exempted, so the aggregate fee actually collected is `0` instead of `Pips::fee_ceil(fee, N)`.

### Finding Description
The broken binding: for a signer moving total `T = -N` (N>1) of a given Nep245/Imt `TokenId` in a batch, the protocol should collect
`fees_collected == Pips::fee_ceil(fee, N)` — this equals the fee for a single `TokenDiff{diff: {token_id: -N}}` intent.

In `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:59-79`), the fee is computed per-intent, per-token, from that intent's own `delta`: [1](#0-0) 

and the exemption boundary lives in `token_fee`: [2](#0-1) 

`amount > 1` is required for `Nep245`/`Imt` to be fee-liable; `amount <= 1` returns `Pips::ZERO`.

`Engine::execute_signed_intents` iterates over an arbitrary collection of independently-signed `MultiPayload`s, verifying each signature/nonce and calling `intents.execute_intent` per payload, only reconciling balances at the very end via `Deltas::finalize` (which enforces the zero-sum invariant across matched deposits/withdrawals, not fee amounts): [3](#0-2) [4](#0-3) 

Nothing aggregates the negative deltas on the same `TokenId`/signer across multiple `TokenDiff` intents before computing `token_fee`. An attacker holding `N` units of a Nep245/Imt token signs `N` distinct `TokenDiff` intents (unique nonces), each with `diff = {token_id: -1, ...}`, matched by a counterparty's positive deltas (either one `+N` intent or `N` `+1` intents), and submits them together to `execute_intents`. Each call to `TokenDiff::execute_intent` computes `token_fee(Nep245/Imt, 1, fee) == Pips::ZERO`, so `fees_collected` stays empty for every leg, versus the `Pips::fee_ceil(fee, N)` that would be charged if the same net transfer were expressed as a single `TokenDiff` intent with `delta = -N`. `MultiPayload::verify`, nonce commitment, and `Deltas::finalize`'s balance invariant all still pass because they only check signature validity, nonce uniqueness, and that deposits/withdrawals net to zero — none of them re-aggregate per-token fee liability across intents.

### Impact Explanation
This is a protocol fee bypass: value that should be credited to `fee_collector` (`engine.state.fee_collector()`) never leaves the trading counterparties' balances, i.e., fees are under-collected for any Nep245/Imt-denominated flow that can be decomposed into unit legs. It generalizes to arbitrarily large `N` (batch size limited only by gas/payload size, which is out of scope), making it Critical per the "protocol fees bypassed or over-collected" category and repeatable across any account/Nep245-or-Imt token pair.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs to hold `N` units of a Nep245 or Imt token and a willing (or self-controlled) counterparty account supplying the matching positive deltas — no privileged role, relayer key, or victim key is required. The attacker fully controls intent construction and can trivially split any transfer into unit legs, each independently signed with the attacker's own key, and batch them into one `execute_intents` call. This is a low-cost, deterministic, always-reproducible bypass, not a probabilistic or edge-case exploit.

### Recommendation
Compute Nep245/Imt fee exemption based on the aggregated net negative delta per `(signer_id, token_id)` across the entire batch (e.g., accumulate deltas before fee evaluation, similar to how `Deltas`/`TransferMatcher` already aggregates balance changes), rather than per individual `TokenDiff` intent. Alternatively, remove the `amount <= 1` exemption tier for Nep245/Imt entirely, or make it apply only when the *total* signer-level movement for that token across the whole execution is `<= 1`.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or a `tests/` sandbox test mirroring `tests/src/tests/defuse/intents/token_diff.rs`) comparing fee-collector balances:
1. Setup: signer with `2` units of a `Nep245` token, `fee = Pips::ONE_PERCENT` (or any non-zero fee), counterparty with matching balance to net the diff to zero.
2. Case A (combined): signer signs a single `TokenDiff{diff: {nep245_token: -2, ...}}`; counterparty signs matching `+2`; execute via `execute_signed_intents`; assert `fee_collector` balance `== Pips::fee_ceil(fee, 2)` (non-zero).
3. Case B (split): signer signs two `TokenDiff{diff: {nep245_token: -1}}` intents (distinct nonces); counterparty signs matching `+1`/`+1` (or one `+2`); execute the batch via `execute_signed_intents`; assert `fee_collector` balance `== 0`.
4. Assert Case A's collected fee `!=` Case B's collected fee for the same net `-2` transfer, proving the fee-bypass divergence, using `TokenDiff::token_fee` and `TokenDiffEvent::fees_collected` (`contracts/defuse/core/src/intents/token_diff.rs:69-78,111-118`) as the direct assertion points.

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

**File:** contracts/defuse/core/src/engine/mod.rs (L113-118)
```rust
    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
    }
```
