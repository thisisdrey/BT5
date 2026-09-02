## Answer



### Title
Splitting `TokenDiff` deltas into `|delta| == 1` legs on Nep245/Imt tokens bypasses protocol fees entirely - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` exempts `Nep245`/`Imt` token deltas from any fee whenever the per-intent `amount <= 1`, but `TokenDiff::execute_intent` computes and charges fees **per individual intent**, not on the net aggregate transferred. An attacker (with a cooperating or self-controlled counterparty account) can split any bulk sale of a `Nep245`/`Imt` asset into many single-unit `TokenDiff` intents in one batch; `TransferMatcher::finalize` still nets the whole batch to zero and executes the full real transfer, but total fees collected is `0` instead of `fee_ceil(total_amount)`.

### Finding Description
The broken binding: for a bulk transfer of `N` units of a `Nep245`/`Imt` token at nonzero protocol `fee`, the fee actually owed should equal `fee.fee_ceil(N)` (as would be charged by one intent with `delta = -N`), i.e. `sum(fees_collected over intents) == fee.fee_ceil(N)`. In reality, when the same `N`-unit transfer is expressed as `N` separate `TokenDiff` intents each with `delta = -1`/`+1`: [1](#0-0) 

```rust
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

each intent has `amount == 1`, hits the `Pips::ZERO` arm, and `fee_ceil(1) == 0`. The fee accumulation happens per-intent inside `execute_intent`: [2](#0-1) 

so `sum(fees_collected) == 0 != fee.fee_ceil(N)` for `N > 1`, breaking the equality.

Crucially, `TransferMatcher::finalize` (called at the end of `Deltas::finalize`) only verifies that, per `TokenId`, aggregate deposits equal aggregate withdrawals across the whole batch — it does not know or care whether those deltas came from one intent or many: [3](#0-2) 

So as long as an attacker (using their own second account, or a cooperating counterparty) submits matching `+1`/`-1` legs on the Nep245/Imt token in the same `execute_intents` batch, the invariant still balances (`Transfers` computed, balances moved for real), but the fee collector receives nothing on that leg. This is a strict per-intent granularity flaw, not a balance-integrity flaw: no guard (`MultiPayload::verify`, nonce checks, `assert_one_yocto`, etc.) inspects cross-intent aggregate amounts to close this gap.

### Impact Explanation
Protocol fee revenue on any `Nep245`/`Imt` (multi-token/semi-fungible) asset trade can be reduced to zero by chunking the trade into unit-sized `TokenDiff` legs, matched against a real counterparty (or attacker's own second account) in the same batch. This directly matches the "protocol fees bypassed" Critical category: legitimate value still moves between accounts as intended by both signers, but the `fee_collector` is under-collected by up to the entire fee that should have applied, for tokens nominally priced with `fee > 0`. It is repeatable across every batch, every Nep245/Imt token, and any account willing to split orders (no bound on number of times it can be exploited).

### Likelihood Explanation
The attacker needs only to control the wallet(s) submitting the intents (or find any willing counterparty to complete an atomic multi-leg swap) — no privileged role, relayer key, or DAO action is required. The only cost is additional intents/signatures in the batch (more payload/gas), which is a normal, unrestricted operation via `execute_intents`/`simulate_intents`. This is trivially and repeatably exploitable by anyone routing large Nep245/Imt trades through their own or a partner order-matching flow.

### Recommendation
Compute the Nep245/Imt fee exemption based on the net aggregate delta per `(signer, token_id)` across the whole batch (or track cumulative `amount` seen for that token/signer within `execute_intents`) rather than per individual `TokenDiff` intent, so that splitting a transfer into unit legs cannot change the total fee owed. Alternatively, remove the `amount > 1` fee-exemption branch for `Nep245`/`Imt` (semi-fungible tokens with genuinely variable quantity) and keep it only for `Nep171` (single NFTs, where quantity is always 1 by construction).

### Proof of Concept
`cargo test` in `contracts/defuse/core/src/intents/token_diff.rs` (or a `near-workspaces` sandbox test under `tests/src/tests/defuse/intents/token_diff.rs`):
1. Set `protocol_fee = Pips::ONE_PERCENT` (nonzero).
2. Construct two accounts A and B with real balances of a `Nep245` `TokenId`.
3. Batch 1: A submits one `TokenDiff{ diff: {X: -1000} }` paired with B's `{X: +1000}` (plus whatever counter-leg token balances the trade) — assert `fees_collected` for A's X leg equals `protocol_fee.fee_ceil(1000) > 0`.
4. Batch 2: A submits 1000 separate `TokenDiff` intents each `{X: -1}` (with matching nonces), paired with B's 1000 intents each `{X: +1}`, all executed in one `execute_intents` call.
5. Assert `TransferMatcher::finalize` succeeds and moves the same net 1000 units of X from A to B (`Transfers` equivalent to batch 1).
6. Assert `sum(fees_collected across all 1000 intents) == 0`, proving the divergence from step 3's nonzero fee for an economically identical transfer.

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
