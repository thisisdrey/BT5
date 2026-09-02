## Analysis

The claimed binding: `fee_collector` balance credited for token `T` after executing two `TokenDiff` intents each with `{T: -1}` should equal the balance credited after one `TokenDiff` intent with `{T: -2}`, when `Pips::fee_ceil(2) > 0`. I traced this and confirmed the two sides genuinely diverge, and no existing guard prevents it.

`TokenDiff::execute_intent` computes the fee **per intent, per token-delta**, independently for each call: for a negative delta it calls `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` where `amount = delta.unsigned_abs()` [1](#0-0) . `token_fee` explicitly exempts `Nep245`/`Imt` token diffs when `amount <= 1`, returning `Pips::ZERO` (comment: "do not take fees on NFTs and MTs with `|delta| <= 1`") [2](#0-1) . This threshold check is evaluated fresh for each `TokenDiff` intent instance — there is no aggregation of a signer's multiple `TokenDiff` intents on the same token within a batch before the threshold check is applied.

`DefuseIntents::execute_intent` iterates the `Vec<Intent>` and calls `execute_intent` on each independently [3](#0-2) , and `Engine::execute_signed_intent` similarly processes each signed `MultiPayload` (and thus each contained `TokenDiff` intent) independently, with no cross-intent fee bookkeeping [4](#0-3) . Therefore:

- One intent `{T: -2}`: `fee = token_fee(T, 2, fee).fee_ceil(2)`. For `Nep245`/`Imt`, since `amount=2 > 1`, `token_fee` returns the full protocol `fee`, so `fee_ceil(2)` is nonzero for e.g. `Pips::ONE_PERCENT` [2](#0-1) .
- Two intents each `{T: -1}`: each call computes `amount=1`, hits the `amount <= 1` exemption branch, returns `Pips::ZERO`, so `fee_ceil(1) = 0` for each, total `0` [5](#0-4) .

The fee actually collected is credited via `internal_add_balance(fee_collector, fees_collected)` only when `fees_collected` is non-empty [6](#0-5) , and `internal_add_balance` on the `Deltas<S>` wrapper both updates the underlying balance and records a `deposit` into the `TransferMatcher` to keep the batch invariant closed [7](#0-6) . Nothing in `TransferMatcher::finalize` re-derives or corrects the fee based on aggregate deltas per signer/token across intents — it only matches deposits against withdrawals to produce `Transfers`, agnostic to how the fee was computed [8](#0-7) .

I did not find any code that aggregates a signer's `TokenDiff` deltas across multiple intents in the same batch, nor any per-batch/per-signer fee normalization for MT/NFT/IMT token types. The `closure_*` helper functions in `token_diff.rs` (used by test fixtures/solvers to compute a counterparty amount) also compute fee per individual delta value, consistent with, not correcting, this per-intent granularity [9](#0-8) .

This matches the "protocol fees bypassed" Critical impact category from the rules. The mechanism is real and reproducible: an unprivileged signer who holds ≥2 units of any `Nep245`/`Imt` token can always split a size-N transfer into N separate unit-amount `TokenDiff` intents (all signed in one `MultiPayload` batch, or across multiple batches) to pay zero fee instead of `Pips::fee_ceil(N)`, provided each unit leg is matched by a corresponding counterparty deposit within the batch (otherwise `TransferMatcher::finalize` returns `InvariantViolated::UnmatchedDeltas`) [10](#0-9) .

### Title
NEP-245/IMT `TokenDiff` fee can be bypassed by splitting an amount>1 delta into unit (amount==1) legs - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` exempts `Nep245`/`Imt` deltas from fees only when `amount <= 1`, and this check is applied independently per `TokenDiff` intent rather than on the aggregate signer/token delta in a batch. A signer moving `N>1` units of a NEP-245/IMT token can split it into `N` intents of `amount==1` each, paying zero protocol fee instead of `Pips::fee_ceil(N)`.

### Finding Description
Binding claimed: `fee_collector.balance_after(two intents {T:-1},{T:-1}) == fee_collector.balance_after(one intent {T:-2})`. This is false whenever `Pips::fee_ceil(2) > 0`.

Root cause: in `TokenDiff::execute_intent`, for each `(token_id, delta)` pair in `self.diff`, if `delta < 0`, the fee is `Self::token_fee(token_id, delta.unsigned_abs(), protocol_fee).fee_ceil(amount)` [1](#0-0) . `token_fee` returns `Pips::ZERO` for `Nep245`/`Imt`/`Nep171` whenever `amount <= 1` [2](#0-1) . Because `execute_intent` is invoked once per `Intent` in the batch (`DefuseIntents::execute_intent` at [3](#0-2) , `Engine::execute_signed_intent` at [4](#0-3) ), the `amount` used in the threshold check is always the size of the individual intent's delta, never the sum of a signer's deltas on that token across the batch.

Attacker's exact payload: sign two `MultiPayload`s (or one `MultiPayload` with two `TokenDiff` intents) each containing `TokenDiff{ diff: {T: -1}, ... }`, matched in the same `execute_intents` batch by a counterparty depositing `+1` twice (or `+2` once) of `T`. Compare to signing one `TokenDiff{ diff: {T: -2}, ... }`.

Existing guards checked and found insufficient: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize` all operate correctly but are orthogonal to fee computation — `finalize` only ensures deposits/withdrawals net to zero, it does not know or care whether the correct fee was taken [10](#0-9) . There is no `checked_*` or aggregation logic anywhere that sums a signer's per-token deltas across multiple intents before the `amount > 1` fee-exemption check.

### Impact Explanation
`fee_collector` under-collects protocol fees on every NEP-245/IMT-based swap or transfer where a signer (or a colluding pair of signers) is willing to split a multi-unit delta into unit legs. This is repeatable per account, per token, per batch, with no cap — an attacker moving any quantity `N` of an MT/IMT asset can always pay `0` fee instead of `fee_ceil(N)` by issuing `N` intents of `amount==1`. This matches the "protocol fees bypassed" Critical category, since the fee_collector's expected credited balance (an amount rightfully owed to the protocol) is not received.

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged signer's control: hold ≥2 units of any `Nep245`/`Imt` `TokenId`, and a nonzero protocol fee for that token type (e.g., `Pips::ONE_PERCENT`, a normal DAO-set fee, not attacker-controlled but a typical configuration). The attacker only needs to be able to sign multiple `MultiPayload`s (trivial, self-signed) and have a counterparty (which can be the attacker's own second account, or a self-matching pair of intents) to close the `TransferMatcher` invariant. Cost is just extra transaction/intent overhead (splitting into more, smaller legs), which is cheap relative to any nontrivial-sized MT/IMT swap. This is fully repeatable across all NEP-245/IMT tokens and all signers.

### Recommendation
Compute the NEP-245/IMT fee-exemption threshold on the aggregated signer+token delta across the whole batch (or across all `TokenDiff` intents for that signer/token within `execute_signed_intents`), not per individual `TokenDiff` intent. Concretely, accumulate `TokenDeltas` per signer across all `TokenDiff` intents in the batch first, then apply `token_fee`'s `amount > 1` check to the aggregated `|delta|` before charging fees, ensuring `fee(split into legs) == fee(single intent)` for the same net delta.

### Proof of Concept
```rust
// cargo test -p defuse-tests (near-workspaces sandbox)
#[rstest]
#[tokio::test]
async fn nep245_fee_bypass_by_splitting(
    #[with(Env::builder().fee(Pips::ONE_PERCENT))]
    #[future(awt)]
    env: Env,
) {
    // setup: attacker + counterparty each hold Nep245TokenId balances,
    // attacker holds >=2 units of token T, counterparty supplies +2 T total.

    // Scenario A: one TokenDiff intent { T: -2 } matched by counterparty { T: +2 }
    let fee_collector_before_a = mt_balance(&env, fee_collector, &T).await;
    execute_intents(vec![attacker_signed_minus2, counterparty_signed_plus2]).await;
    let fee_collector_after_a = mt_balance(&env, fee_collector, &T).await;
    let fee_a = fee_collector_after_a - fee_collector_before_a;
    assert_eq!(fee_a, Pips::ONE_PERCENT.fee_ceil(2)); // nonzero

    // Scenario B: two TokenDiff intents { T: -1 } each, matched by two { T: +1 }
    let fee_collector_before_b = mt_balance(&env, fee_collector, &T).await;
    execute_intents(vec![
        attacker_signed_minus1_leg1, counterparty_signed_plus1_leg1,
        attacker_signed_minus1_leg2, counterparty_signed_plus1_leg2,
    ]).await;
    let fee_collector_after_b = mt_balance(&env, fee_collector, &T).await;
    let fee_b = fee_collector_after_b - fee_collector_before_b;
    assert_eq!(fee_b, 0);

    // Binding violated: same net -2 delta moved, but fee_a != fee_b
    assert_ne!(fee_a, fee_b);
}
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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L163-216)
```rust
    pub fn closure_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        Self::closure_supply_delta(token_id, Self::supply_delta(token_id, delta, fee)?, fee)
    }

    /// Returns total supply delta from token delta
    #[inline]
    fn supply_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        if delta < 0 {
            // fee is taken only on negative deltas (i.e. token_in)
            delta.checked_mul_div_ceil(
                Self::token_fee(token_id, delta.unsigned_abs(), fee)
                    .invert()
                    .as_pips()
                    .into(),
                Pips::MAX.as_pips().into(),
            )
        } else {
            // token_out
            Some(delta)
        }
    }

    /// Returns closure for total supply delta that should be given in
    /// a single [`TokenDiff`] to successfully execute [`TokenDiff`] with
    /// given `delta` on the same token assuming given `fee`.
    #[inline]
    pub fn closure_supply_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        let closure = delta.checked_neg()?;
        if closure < 0 {
            // fee is taken only on negative deltas (i.e. token_in)
            closure.checked_mul_div_euclid(
                Pips::MAX.as_pips().into(),
                Self::token_fee(token_id, delta.unsigned_abs(), fee)
                    .invert()
                    .as_pips()
                    .into(),
            )
        } else {
            // token_out
            Some(closure)
        }
    }

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L136-149)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_add_balance(owner_id.clone(), [(token_id.clone(), amount)])?;
            if !self.deltas.deposit(owner_id.clone(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
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
