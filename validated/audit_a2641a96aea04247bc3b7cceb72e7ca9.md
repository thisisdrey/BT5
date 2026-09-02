### Title
`TokenDiff` NEP-245/IMT fee exemption bypassed by splitting a large trade into many `amount == 1` intents in one signed payload - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::execute_intent` computes and charges the protocol fee independently for each `TokenDiff` intent in the batch, using only that single intent's own `delta` magnitude to decide the NEP-245/NEP-171/IMT `amount <= 1` fee exemption. Because one signed `MultiPayload` may contain an arbitrary number of `TokenDiff` intents for the same signer, an attacker can split what is effectively one large NEP-245 (or IMT) trade into N unit (`amount == 1`) `TokenDiff` intents, each individually exempted from the fee, while `TransferMatcher::finalize` aggregates all these unit deltas into one real transfer of size N with the counterparty.

### Finding Description
The broken binding: fee charged and credited to `fee_collector` for token `X` under signer `A` within one call to `execute_intents` should equal `protocol_fee` applied to the **aggregate** negative delta moved for `(A, X)` in that call — i.e. `sum(fees_collected[X])` over the batch `==` `fee_ceil(protocol_fee, |sum(delta_X)|)` when the resulting real transfer for token `X` is a single NEP-245/IMT transfer of size `|sum(delta_X)|`. Instead, the code computes fee per-intent: [1](#0-0) 

and the exemption check is evaluated on the individual intent's `amount = delta.unsigned_abs()`: [2](#0-1) 

`DefuseIntents::execute_intent` simply loops over `self.intents` and calls `execute_intent` on each in sequence, all under the same `signer_id` from a single signature/nonce: [3](#0-2) 

There is no restriction preventing the same `TokenId` from appearing across multiple distinct `TokenDiff` intents inside one `DefuseIntents.intents` vector, and no aggregation of `amount` across intents before applying the exemption.

Meanwhile, downstream, `TransferMatcher`/`TokenTransferMatcher` (in `contracts/defuse/core/src/engine/state/deltas.rs`) accumulates all deltas per `(owner, token)` across the whole batch and produces a single netted physical transfer: [4](#0-3) [5](#0-4) 

So the counterparty's matched real transfer is exactly as large as it would be for one big trade, but the fee-exemption check on the attacker's side sees only `amount == 1` on each of the N intents composing that trade, so `Pips::ZERO` is returned N times and the fee is fully bypassed.

**Attacker's exact payload**: sign one `MultiPayload` (one nonce, one signature) whose `DefuseIntents.intents` contains N `TokenDiff` intents, each `{ diff: { nep245_token_id: -1, some_other_token: +k } }`, plus a counterparty `MultiPayload` (can be the attacker's own second account) with a single `TokenDiff` intent `{ nep245_token_id: +N, some_other_token: -k*N }` (or itself split, irrelevant since fee is only taken from negative deltas). Both payloads are passed together to `execute_intents`. The N unit withdrawals aggregate in `TokenTransferMatcher` to a single N-unit transfer, but `fees_collected` computed inside each `TokenDiff::execute_intent` call is `Pips::ZERO` for every one of the N intents (since `amount == 1` triggers the NFT/MT exemption), producing zero total fee versus the `protocol_fee` that would be owed on a single unsplit `TokenDiff{nep245_token_id: -N}`.

Existing guards (`MultiPayload::verify`, `verify_intent_nonce`, `commit_nonce`, `TransferMatcher::finalize`'s zero-sum invariant, `Pips::fee_ceil`) do not prevent this: they check signature validity, nonce replay, and that the batch's deltas net to zero — none of them re-derive or aggregate the fee across intents for the same `(signer, token)` pair.

### Impact Explanation
Protocol fee is systematically bypassed for any NEP-245 (or IMT) token trade, regardless of size, by unit-splitting the negative side of the trade into `amount == 1` `TokenDiff` intents within a single signed payload. This is a direct "protocol fees bypassed" case, explicitly listed as Critical impact. It is fully repeatable across accounts, tokens, and batches — the attacker only needs to control the signer of the split side (and either control or coordinate with the counterparty account providing the matching liquidity), and can apply it to every NEP-245/IMT trade they make, at zero incremental cost beyond intent count in the payload.

### Likelihood Explanation
- No privileged role or victim key is required; the attacker only needs two accounts (both can be attacker-owned) with existing Verifier balances of the traded tokens.
- The attacker needs to produce N `TokenDiff` intents inside one `DefuseIntents` list and sign it once — this is a purely client-side payload construction, requiring no on-chain privilege.
- The `amount` threshold in `token_fee` is a compile-time constant (`<= 1`), so `amount == 1` unit splitting always triggers the exemption; this is deterministic and always reproducible.
- The only limiting factor is transaction/message size and gas for a large N, which is out-of-scope per the rules (DoS/gas exhaustion excluded), but even modest fee savings per batch (any N > 1) already demonstrate systematic under-collection.

### Recommendation
Aggregate the total negative delta per `(signer_id, token_id)` across **all** `TokenDiff` intents within a single `DefuseIntents`/batch execution before applying `TokenDiff::token_fee`'s NFT/MT exemption and computing `fee_ceil`, rather than evaluating and charging the fee independently per individual `TokenDiff` intent. Concretely, accumulate `amount` per `(signer_id, token_id)` in `Engine`/`Deltas` state across the whole `execute_signed_intents` call (or at minimum across one `DefuseIntents.intents` vector) and compute the fee once against that aggregate amount, deferring collection until the full negative-delta magnitude for that pair is known.

### Proof of Concept
`cargo test` (near-workspaces sandbox, in `tests/src/tests/defuse/intents/token_diff.rs`):
1. Set `fee = Pips::ONE_PERCENT` via `Env::builder().fee(fee)`.
2. Create two users A (attacker) and B (counterparty/attacker's own second account), and an NEP-245 multi-token contract; deposit `N` units of `mt_token` to A and sufficient `ft_token` to B.
3. Build one `MultiPayload` for A containing N `TokenDiff` intents, each `{ diff: {mt_token: -1, ft_token: +price} }`.
4. Build one `MultiPayload` for B containing a single `TokenDiff` intent `{ diff: {mt_token: +N, ft_token: -price*N} }` (matching the aggregate).
5. Call `execute_intents([payload_A, payload_B])`.
6. Assert on both sides of the binding:
   - LHS (actual): total fee credited to `fee_collector` for `mt_token` == `0` (bug reproduced) because each of the N `TokenDiff` intents saw `amount == 1`.
   - RHS (expected/correct binding): `Pips::ONE_PERCENT.fee_ceil(N)` should have been charged, since the aggregate real transfer of `mt_token` (verified via `Transfers`/`mt_balance_of` on B) is `N`.
   - Show LHS ≠ RHS for N > 1 (e.g., N = 100), demonstrating the fee bypass; contrast with a single unsplit `TokenDiff{mt_token: -N}` from A, which does charge `Pips::ONE_PERCENT.fee_ceil(N)` correctly (per existing `swap_many`-style tests using `TokenDiff::closure_delta`).

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

**File:** contracts/defuse/core/src/intents/mod.rs (L97-112)
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
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L306-333)
```rust
    #[inline]
    pub fn add_delta(&mut self, owner_id: AccountId, delta: i128) -> bool {
        let amount = delta.unsigned_abs();
        if delta.is_negative() {
            self.withdraw(owner_id, amount)
        } else {
            self.deposit(owner_id, amount)
        }
    }

    fn sub_add(
        sub: &mut AccountAmounts,
        add: &mut AccountAmounts,
        owner_id: AccountId,
        mut amount: u128,
    ) -> bool {
        let s = sub.amount_for(&owner_id);
        if s > 0 {
            let a = s.min(amount);
            sub.sub(owner_id.clone(), a)
                .unwrap_or_else(|| unreachable!());
            amount = amount.saturating_sub(a);
            if amount == 0 {
                return true;
            }
        }
        add.add(owner_id, amount).is_some()
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
