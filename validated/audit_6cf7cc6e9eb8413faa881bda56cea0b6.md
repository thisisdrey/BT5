### Title
Protocol fees on NEP-245/IMT tokens bypassed via unit-size (`amount == 1`) diff structuring - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` tokens whenever the per-intent delta magnitude is `<= 1` [1](#0-0) . Because this check is evaluated independently for every `TokenDiff` intent inside `execute_intent`, an attacker can move an arbitrarily large aggregate amount of a fungible NEP-245/IMT token by splitting it into many separately-signed, unit-size (`delta == -1`) `TokenDiff` payloads submitted together in one `execute_intents` call, paying zero fee overall instead of `fee.fee_ceil(k)`.

### Finding Description
The broken binding: `fees_collected[T]` after a batch that moves `k` units of token `T` out of the signer's account should equal `fee.fee_ceil(k)` (what a single `TokenDiff{T: -k}` intent would charge), but with structuring it equals `0`.

Root cause, in `TokenDiff::execute_intent`:
```
for (token_id, delta) in &self.diff {
    ...
    if *delta < 0 {
        let amount = delta.unsigned_abs();
        let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
        fees_collected.add(token_id.clone(), fee)...
    }
}
``` [2](#0-1) 

and `token_fee`:
```
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
``` [1](#0-0) 

`amount` here is the magnitude of a *single intent's* delta on a single token id, not any aggregate over the account, token, or batch. `execute_signed_intents` processes each signed `MultiPayload` (each with its own verified nonce, so `verify_intent_nonce`/`commit_nonce` correctly prevent replay of a single signature) independently, calling `TokenDiff::execute_intent` per intent, before finally checking the global zero-sum invariant via `Deltas::finalize` → `TransferMatcher::finalize` [3](#0-2) [4](#0-3) .

Exploit flow: the attacker (or attacker + a cooperating counterparty account) holds balance `k` of a fungible-style `Nep245TokenId`. Instead of one signed `TokenDiff{diff: {T: -k, ...}}` (which would trigger the `amount > 1` branch and pay `fee.fee_ceil(k)`), the attacker signs `k` distinct `MultiPayload`s, each a `TokenDiff{diff: {T: -1, ...}}` with a fresh nonce, and submits them all in a single `execute_intents([payload_1..payload_k])` call along with matching counterparty intent(s) providing the reciprocal `+k` (the receiving side never pays a fee regardless of amount, since fees are only charged on negative deltas). Because `TransferMatcher` aggregates deposits/withdrawals across the whole batch per token id before matching (not per-intent-pair) [5](#0-4) , the counterparty does not need to split their side — only the fee-liable `-1` side needs splitting. The batch nets to zero and passes `finalize()`, yet `fees_collected` for `T` is `0` across all `k` intents instead of `fee.fee_ceil(k)`.

None of the existing guards prevent this: `MultiPayload::verify`, nonce commit, and `TransferMatcher::finalize`'s zero-sum check all operate correctly and are orthogonal to fee computation — the invariant they enforce is about balance conservation, not fee correctness, and fee amounts (via `internal_add_balance` to the fee collector) are themselves folded into the same zero-sum ledger, so a batch with zero fee still balances fine.

### Impact Explanation
Protocol fee revenue on all NEP-245/IMT (multi-token/intent-mintable-token) assets can be reduced to zero regardless of trade size, by any unprivileged signer, purely through client-side transaction structuring (no contract bug beyond the fee-classification logic). This is a systematic under-collection of protocol fees, matching the Critical category "protocol fees bypassed or over-collected." It is fully repeatable across all NEP-245/IMT token ids, all accounts, and unlimited batches (bounded only by gas/payload-count practicality, which is explicitly out of scope to disregard as DoS/resource concerns).

### Likelihood Explanation
Preconditions are minimal and entirely within an unprivileged signer's control: the attacker needs their own Verifier balance of the token being traded, the ability to sign `k` off-chain `DefusePayload`s with distinct nonces (no gas cost until submission), and a willing counterparty (which can be the attacker's own second account) to provide the offsetting `+k` delta. No privileged role, relayer key, or victim key is required. The only cost is `k` signatures (cheap, off-chain) and the gas of one `execute_intents` call with `k` payloads. This is highly feasible and trivially repeatable.

### Recommendation
Compute the NEP-245/IMT fee exemption based on an aggregate delta per `(signer, token_id)` across the whole batch (e.g., accumulate deltas before evaluating `token_fee`, or move the `amount <= 1` check to operate on the total netted delta per token per batch rather than per individual `TokenDiff` intent). Alternatively, remove the per-intent exemption entirely and base the fee-exemption strictly on whether the token id represents a true 1-of-1 NFT (max supply/decimals known to be 1), rather than an arbitrary per-call delta magnitude.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (unit-level, using `Engine`/`Deltas` test harness similar to `contracts/defuse/core/src/engine/state/deltas.rs` tests):
1. Set `protocol_fee = Pips::ONE_PERCENT` (or any non-zero fee), fee_collector = `fee.near`.
2. Give `user.near` balance `100` of `Nep245TokenId("mt.near", "token1")` and `receiver.near` balance `0` of the same, with a corresponding `+100` intent for `receiver.near`.
3. Scenario A (baseline): execute one `TokenDiff{ diff: {T: -100} }` from `user.near` matched by `TokenDiff{ diff: {T: +100} }` from `receiver.near` in a single `execute_signed_intents` batch. Assert `fees_collected` for `T` == `fee.fee_ceil(100)` (> 0), and fee_collector's balance of `T` increases by that amount.
4. Scenario B (structuring): execute 100 separate `TokenDiff{ diff: {T: -1} }` intents from `user.near` (each a distinct signed payload with a distinct nonce) plus one `TokenDiff{ diff: {T: +100} }` from `receiver.near`, all in one `execute_signed_intents`/`execute_intents` call. Assert the batch succeeds (`finalize()` invariant holds, zero unmatched deltas) and the sum of `fees_collected` for `T` across all 100 `TokenDiffEvent`s == `0`, while `user.near`'s balance decreased by 100 and `receiver.near`'s increased by 100 — identical net transfer to Scenario A but with `fee_collector` balance unchanged, proving `0 != fee.fee_ceil(100)`.

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L337-391)
```rust
    pub fn finalize_into(self, token_id: &TokenId, transfers: &mut Transfers) -> Result<(), i128> {
        // sort deposits and withdrawals in descending order
        let [mut deposits, mut withdrawals] = [self.deposits, self.withdrawals].map(|amounts| {
            let mut amounts: Vec<_> = amounts.into_iter().collect();
            amounts.sort_unstable_by_key(|(_, amount)| Reverse(*amount));
            amounts.into_iter()
        });

        // take first sender and receiver
        let (mut deposit, mut withdraw) = (deposits.next(), withdrawals.next());

        // as long as there is both: sender and receiver
        while let Some(((sender, send), (receiver, receive))) =
            withdraw.as_mut().zip(deposit.as_mut())
        {
            // get min amount and transfer
            let transfer = (*send).min(*receive);
            transfers
                .transfer(sender.clone(), receiver.clone(), token_id.clone(), transfer)
                // no error can happen since we add only one transfer for each
                // combination of (sender, receiver, token_id)
                .unwrap_or_else(|| unreachable!());

            // subtract amount from sender and receiver
            *send = send.saturating_sub(transfer);
            *receive = receive.saturating_sub(transfer);

            if *send == 0 {
                // select next sender
                withdraw = withdrawals.next();
            }
            if *receive == 0 {
                // select next receiver
                deposit = deposits.next();
            }
        }

        // only sender(s) left
        if let Some((_, send)) = withdraw {
            return Err(withdrawals
                .try_fold(send, |total, (_, s)| total.checked_add(s))
                .and_then(|total| i128::try_from(total).ok())
                .and_then(i128::checked_neg)
                .unwrap_or_default());
        }
        // only receiver(s) left
        if let Some((_, receive)) = deposit {
            return Err(deposits
                .try_fold(receive, |total, (_, r)| total.checked_add(r))
                .and_then(|total| i128::try_from(total).ok())
                .unwrap_or_default());
        }

        Ok(())
    }
```
