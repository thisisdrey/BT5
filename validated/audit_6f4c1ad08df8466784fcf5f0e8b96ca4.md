## Title
Protocol fee bypass on NEP-245/IMT multi-token trades via unit-splitting `TokenDiff` intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

## Summary
`TokenDiff::execute_intent` computes and charges the protocol fee independently per `TokenDiff` intent, using `TokenDiff::token_fee` which exempts NEP-245/IMT deltas with `amount <= 1` from any fee. Because `DefuseIntents::execute_intent` simply loops over each intent and executes it in isolation, while `TransferMatcher` aggregates all resulting balance deltas for the same `(token_id)` across the *entire* signed payload into one real transfer, an attacker can split a single large multi-token (NEP-245) trade into many `TokenDiff` intents each moving exactly `amount == 1` of the same token, causing the aggregate executed transfer to be arbitrarily large while the total fee collected is zero.

## Finding Description
The broken binding is:
`fees_collected(fee_collector, token) == protocol_fee.fee_ceil(aggregate_signed_amount_moved(signer, token))`

but the actual code enforces only:
`fees_collected += token_fee(token_id, |delta_i|, protocol_fee).fee_ceil(|delta_i|)` computed **per individual `TokenDiff` intent** `i`, at [1](#0-0) .

`TokenDiff::token_fee` explicitly zeroes the fee whenever the per-intent `amount <= 1` for `Nep171`/`Nep245`/`Imt` token types: [2](#0-1) 

`DefuseIntents::execute_intent` executes each `Intent` (including each `TokenDiff`) independently, with no consolidation of deltas by `(signer, token)` before the fee is computed: [3](#0-2) 

However, the balance changes produced by every `internal_add_balance`/`internal_sub_balance` call (regardless of which intent produced them) are funneled into a single `TransferMatcher` for the whole call, which aggregates deposits/withdrawals per `token_id` across *all* intents and produces one net `Transfers` entry via `finalize`: [4](#0-3) [5](#0-4) 

**Exploit flow:** A signer wants to move `N` units of a NEP-245 `Nep245TokenId` (or IMT) to a counterparty in exchange for some other token, where `N > 1` would normally incur `protocol_fee.fee_ceil(N)`. Instead of signing one `TokenDiff{diff: {token: -N, other_token: +M}}`, the attacker signs a single `MultiPayload`/`DefuseIntents` containing `N` separate `TokenDiff` intents, each with `diff: {token: -1, other_token: +m_i}` (summing `m_i` to `M`). Each intent independently calls `token_fee(token, amount=1, fee)`, which returns `Pips::ZERO`, so no fee is ever added to `fees_collected` for any of the `N` sub-intents. The counterparty can still supply one (or matching) offsetting `TokenDiff`(s) for the aggregate `N`/`M`, and `TransferMatcher::finalize` merges all the unit-level deltas into the same large real transfer that would have resulted from a single unsplit `TokenDiff`. The fee-exemption check that was designed for atomic single-unit NFT/MT transfers is evaluated at the wrong granularity (per-intent instead of per aggregate transfer), so it is trivially defeated by fragmenting one logical trade across multiple intents inside the same signed payload (single nonce, single signature).

No existing guard catches this: `MultiPayload::verify`/nonce checks only ensure the payload is validly signed once; `TransferMatcher::finalize` only checks that deltas net to zero across the batch, it does not re-derive or re-check fees; there is no per-signer/per-token fee aggregation anywhere in the intent execution path.

## Impact Explanation
The `fee_collector` account is under-credited (receives `0` fee) for what is, in net effect, an arbitrarily large NEP-245/IMT token movement, while the counterparty's Verifier balance changes exactly as if a full fee-bearing trade had occurred. This directly matches the Critical category "protocol fees bypassed" — value (the fee) that should leave the trading parties and go to `fee_collector` never does, and the loss is realized against every configured non-zero fee on NEP-245/IMT-denominated trades. The attack is fully repeatable across any account, any NEP-245/IMT token id, and any batch, bounded only by gas/transaction size limits for how many intents can be packed into one payload.

## Likelihood Explanation
The attacker needs no special role: any two unprivileged accounts (attacker plus a willing counterparty, who may even be the same attacker controlling a second key or colluding with a second account) with a NEP-245/IMT balance in the Verifier can construct such a payload. No deposits, no privileged calls, and no interaction with escrow-swap are required — this is a pure `execute_intents`/`simulate_intents` construction attack. The only cost is transaction gas to include `N` intents, which is off-chain-constructible and scales linearly with the amount to be laundered fee-free, making it economically attractive whenever `protocol_fee * N` exceeds the marginal gas cost of extra intents.

## Recommendation
Compute and apply the NEP-245/IMT fee exemption based on the *aggregate* net delta per `(signer_id, token_id)` across the whole `DefuseIntents`/payload (e.g., by deferring fee calculation until after all `TokenDiff` deltas for the call have been accumulated per signer/token, mirroring how `TransferMatcher` aggregates transfers), rather than evaluating `amount <= 1` on each individual `TokenDiff` intent in isolation.

## Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Deploy Defuse with `fee = Pips::ONE_PERCENT`, create two users `attacker` and `counterparty`, and a NEP-245 multi-token contract; deposit `N = 100` units of `nep245_token_id` to `attacker`'s Verifier balance and enough of `ft_out` to `counterparty`.
2. Build a single `DefuseIntents` containing `N = 100` `TokenDiff` intents from `attacker`, each `{diff: {nep245_token_id: -1, ft_out: +TokenDiff::closure_delta(ft_out, +1, fee)}}`; sign as one `MultiPayload`.
3. Build one offsetting `TokenDiff` from `counterparty`: `{diff: {nep245_token_id: +100, ft_out: -100}}` (aggregate closure), signed as its own `MultiPayload`.
4. Call `execute_intents` with both payloads.
5. Assert (a) `attacker`'s `nep245_token_id` balance decreased by 100 and `counterparty`'s increased by 100 (confirming `TransferMatcher` aggregated the unit transfers into one real 100-unit transfer), and (b) `fee_collector`'s balance for `nep245_token_id` is `0` instead of the expected `Pips::ONE_PERCENT.fee_ceil(100)`, i.e. `fees_collected_total != protocol_fee.fee_ceil(100)`, demonstrating the fee-bypass binding violation.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-78)
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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L136-164)
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

    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_sub_balance(owner_id, [(token_id.clone(), amount)])?;
            if !self.deltas.withdraw(owner_id.to_owned(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
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
