### Title
Protocol fee bypassed on `Nep245`/`Imt` `TokenDiff` legs via unit-delta splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives the protocol fee whenever the absolute delta on a `Nep245` or `Imt` token in a single intent is `<= 1`, regardless of the token's actual fungibility. Because fees are computed per-`TokenDiff` intent rather than on the net amount of the logical trade, an unprivileged signer can split one intended transfer of `N` units of an MT token into `N` separate `TokenDiff` intents (distinct nonces, one `delta == -1` each, matched by a counterparty's `+1` legs via `TransferMatcher`), collecting zero fee on every leg instead of the nonzero fee that a single `delta == -N` intent would incur.

### Finding Description
The broken binding: `Σ_{i=1}^{N} fees_collected_i` (from executing `N` `TokenDiff` intents each with `delta = -1` on the same `Nep245` `TokenId`) should equal `Pips::fee_ceil(fee, N)` — the fee that a single `TokenDiff` with `delta = -N` on that token would owe — but does not.

Code path:
- `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:59-79`) applies each `(token_id, delta)` pair independently via `internal_apply_deltas`, and for negative deltas computes `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` per intent, with no aggregation across intents, nonces, or the batch.
- `TokenDiff::token_fee` (`contracts/defuse/core/src/intents/token_diff.rs:206-216`) explicitly returns `Pips::ZERO` for `Nep171`/`Nep245`/`Imt` token types whenever `amount <= 1`: `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}` else `return Pips::ZERO`.
- `Pips::fee_ceil` (`crates/primitives/fees/src/lib.rs:116-121`) uses ceiling division, so for any `fee > 0` and `amount >= 1` with `amount * fee.as_pips() > 0`, `fee_ceil(amount) >= 1` — i.e., a single `delta = -N` (`N > 1`) intent on the same token always yields a strictly positive fee, while `N` split `delta = -1` legs each yield exactly `Pips::ZERO`.
- Batch-level balancing across the `N` split legs and their matched counterpart legs is enforced purely per-token net-zero (not fee-aware) by `TransferMatcher::add_delta`/`finalize` (`contracts/defuse/core/src/engine/state/deltas.rs:261-283`, `295-333`). This mechanism only checks that deposits and withdrawals net to zero per token across the whole `execute_signed_intents` batch — it has no notion of "this token amount was split from one logical transfer," so `N` unit legs summing to `-N`/`+N` pass the invariant exactly like one `-N`/`+N` pair, just without triggering the fee threshold.

Exploit: attacker (optionally colluding with, or acting via, a second account they control as counterparty) constructs `N` `MultiPayload`s, each containing one `TokenDiff{diff: {Nep245Token: -1, OtherToken: +m}}` signed with a fresh nonce, matched against `N` counterparty `TokenDiff` intents with `+1`/`-m` legs, all submitted in one `execute_intents`/`simulate_intents` batch. Each leg's `amount == 1`, so `token_fee` returns `Pips::ZERO` for every leg, and `fees_collected` sums to `0` for the whole batch on that token, versus the nonzero `fee_ceil(fee, N)` that would be charged had the transfer been expressed as one `delta = -N` intent.

None of the existing guards (`MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, nonce commit, `TransferMatcher::finalize`) address this because they validate signature/nonce/replay and batch-level token conservation, not fee sizing; the fee computation itself is the vulnerable, per-intent, threshold-based logic.

### Impact Explanation
This is a protocol-fee-bypass vulnerability (Critical per the stated impact categories: "protocol fees bypassed or over-collected"). Any `Nep245`/`Imt` (multi-token) trade of size `N > 1` can have its protocol fee reduced to zero by decomposing it into `N` unit legs, at the cost of extra transactions/gas and requiring a counterparty (self-controlled account or a permissive solver) willing to sign matching unit legs. This does not move funds without authorization, but it under-collects revenue owed to `fee_collector` on every large MT-token trade, repeatable indefinitely across accounts, tokens, and batches — the fee collector is shorted by `fee_ceil(fee, N)` per bypassed trade.

### Likelihood Explanation
Feasible for any unprivileged signer with a counterparty willing to sign matching `+1` legs (which can be the same attacker using a second self-controlled account, requiring no privileged role, relayer key, or victim key). The only cost is `N` times the signing/transaction overhead versus one intent — no special preconditions, balances, or timing beyond having `N` units of the `Nep245`/`Imt` token to trade. It is directly exploitable today via ordinary `execute_intents`/`simulate_intents` calls with self-crafted `MultiPayload`s.

### Recommendation
Compute the fee threshold on the net signed amount per `(signer, token_id)` across the whole batch (or persist/aggregate token-in amounts across all `TokenDiff` intents from the same signer within one `execute_signed_intents` call) rather than per individual intent, before applying the `amount <= 1` NFT/MT exemption in `TokenDiff::token_fee`. Alternatively, restrict the `amount <= 1` fee exemption to token IDs that are provably non-fungible (e.g., only `Nep171`, or `Nep245`/`Imt` IDs whose total supply is verified to be 1), rather than exempting based on a single leg's transient `delta` magnitude.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (or tests/ sandbox crate)
// 1. Setup: Env with fee = Pips::ONE_PERCENT (nonzero).
// 2. Case A (single intent): user signs one TokenDiff{ diff: {mt_token: -N, other: closure_delta(...)} },
//    counterparty signs matching +N/-closure leg, N > 1 (e.g. N = 10) on a Nep245 TokenId.
//    Execute via execute_intents; read fees_collected for mt_token from emitted TokenDiffEvent /
//    fee_collector balance. Assert fee_A == Pips::ONE_PERCENT.fee_ceil(N) > 0.
// 3. Case B (split): same net trade expressed as N TokenDiff intents, each delta = -1 / +1 on mt_token,
//    each with a distinct nonce, matched pairwise with counterparty unit legs, submitted together
//    in one execute_intents/simulate_intents batch.
//    Assert sum of fees_collected across all N intents for mt_token == 0.
// 4. Binding check: assert fee_A != sum(fees_B), demonstrating fee_ceil(fee, N) (Case A) > 0 == sum (Case B).
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-79)
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

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L261-284)
```rust
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
