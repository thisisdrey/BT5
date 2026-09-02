### Title
Protocol fee bypass on `Nep245`/`Imt` `TokenDiff` swaps via unit-size splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives fees for `Nep245`/`Imt` tokens whenever the *per-intent* `|delta|` is `<= 1`, but `TokenDiff::execute_intent` computes this threshold independently for each signed `TokenDiff` intent, with no aggregation across intents in the same (or a different) `MultiPayload` batch. An unprivileged signer can therefore split any MT/IMT position change into unit-sized (`|delta| == 1`) legs across multiple signed `TokenDiff` intents to make the fee evaluate to `Pips::ZERO` on every leg, entirely avoiding the `fee_ceil` fee that a single combined intent would have paid.

### Finding Description
The binding claimed to hold is:
`fee_collector_balance_after - fee_collector_balance_before == protocol_fee.fee_ceil(2)` for any MT swap moving 2 units of a `Nep245` token with fee `protocol_fee > Pips::ZERO`.

In `contracts/defuse/core/src/intents/token_diff.rs`: [1](#0-0) 
`token_fee` returns `Pips::ZERO` for `Nep245`/`Imt` whenever `amount <= 1`, where `amount` is the `unsigned_abs()` of a *single* intent's delta: [2](#0-1) 

`execute_intent` iterates only over `self.diff` — the deltas of the one `TokenDiff` object being executed — and computes `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` scoped strictly to that intent. There is no cross-intent bookkeeping of cumulative `|delta|` per `(signer, token_id)` for a batch.

`Engine::execute_signed_intents`/`execute_signed_intent` simply verify each signed payload's signature/nonce/deadline and execute its intents sequentially: [3](#0-2) 
The only cross-intent check performed afterwards is the balance/`TransferMatcher` net-zero invariant in `finalize()`, which verifies that total deposits equal total withdrawals per token across the whole batch (including the fee collector's deposit) — it does not verify that the *correct* fee amount was collected: [4](#0-3) 

Exploit: instead of signing one `TokenDiff{ diff: {mt_token: -2, other_token: +Y} }` (which would compute `token_fee(mt_token, 2, fee)` = `fee` and charge `fee.fee_ceil(2) > 0`), the attacker signs two separate `TokenDiff` intents each with `{ mt_token: -1, other_token: +Y/2 }` (optionally from the same or a second self-controlled account acting as counterparty to keep the batch's net-zero invariant satisfied), and submits both in one `execute_intents`/`MultiPayload` batch. Each intent independently computes `token_fee(mt_token, 1, fee) == Pips::ZERO`, so `fee.fee_ceil(1) == 0` on both legs — total fee collected is `0` instead of `fee.fee_ceil(2)`.

None of the existing guards (`MultiPayload::verify`, nonce/salt checks, `TransferMatcher::finalize`) prevent this because they operate on signature validity and net balance conservation, not on fee correctness, and the fee waiver logic itself is scoped per intent by design (intended to avoid rounding NFTs/whole MTs up to a minimum fee), not per aggregate signer position.

### Impact Explanation
The fee collector (`fee_collector`) under-collects protocol fees on every `Nep245`/`Imt` swap that is split into unit-size legs; the value that should have flowed to the fee collector instead stays with the trading parties. This is repeatable without limit: any MT/IMT swap of size `N` can be split into `N` legs of `|delta|=1` to reduce the total protocol fee to `0`, for any `N` and any nonzero `protocol_fee`. This matches the explicitly listed Critical category "protocol fees bypassed... against fee_collector."

### Likelihood Explanation
Preconditions are minimal and fully within the unprivileged attacker's capability: an existing MT (`Nep245`) contract, a deposited balance in the attacker's Verifier account, and `protocol_fee > Pips::ZERO`. No privileged role, relayer key, or DAO action is needed — the attacker only needs to sign multiple ordinary `TokenDiff` intents (possibly using a second self-controlled account as counterparty to keep the batch net-zero) and submit them together via `execute_intents`. This is a cheap, fully repeatable, mechanical bypass usable on every future MT/IMT trade.

### Recommendation
Aggregate `|delta|` per `(signer_id, token_id)` across all `TokenDiff` intents within a single `execute_intents`/`MultiPayload` batch (and ideally scope the "amount <= 1" NFT/MT waiver to true NFT-like semantics, e.g. by token type/max-supply rather than raw delta) before evaluating `token_fee`, so the fee waiver cannot be gamed by splitting a larger position change into multiple unit-sized `TokenDiff` intents.

### Proof of Concept
```rust
// cargo test -p defuse-tests --test <suite> exploit_split_bypasses_mt_fee
// Env: fee = Pips::ONE_PERCENT (or any Pips > ZERO), mt_token deployed, attacker deposits 2 units.

// Combined-intent baseline (not executed, just for the expected value):
let expected_fee = protocol_fee.fee_ceil(2); // > 0

// Split attack: two TokenDiff intents, each delta=-1 on the same Nep245 token_id,
// counterpart legs supplied by a second self-controlled account to satisfy the
// batch's TransferMatcher net-zero invariant.
let intent_a = TokenDiff { diff: {mt_token_id: -1, other_token_id: +y1}, .. };
let intent_b = TokenDiff { diff: {mt_token_id: -1, other_token_id: +y2}, .. };
// counter_a / counter_b: matching +1/-y1 and +1/-y2 legs from a second account.

let signed = [sign(attacker, [intent_a]), sign(counterparty, [counter_a]),
              sign(attacker, [intent_b]), sign(counterparty, [counter_b])];

env.defuse_execute_intents(env.defuse.contract_id(), signed).await.unwrap();

let fee_collector_balance_after = balance_of(fee_collector, mt_token_id);
assert_eq!(fee_collector_balance_after - fee_collector_balance_before, 0); // actual
assert_ne!(0, expected_fee); // shows binding fee_collector_balance_after - before == protocol_fee.fee_ceil(2) is violated
```

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
