## Analysis

**Binding (before tracing):** `fees_collected[T] == Pips::fee_ceil(Σ |negative deltas of T| actually transferred in the batch)`, where the right side is what `TransferMatcher::finalize` ultimately nets into a real transfer.

**Trace:**

`TokenDiff::execute_intent` computes the fee independently per `(token_id, delta)` pair *within a single intent's diff map*, using only that pair's own magnitude: [1](#0-0) 

`token_fee` waives the fee entirely for `Nep245`/`Imt` when `amount <= 1`: [2](#0-1) 

Crucially, the balance change from each leg is fed into `Deltas`/`TransferMatcher` via `internal_sub_balance`/`internal_add_balance`, which **sum** per-account amounts across all intents in the batch into a single `u128` before `finalize_into` computes actual transfers: [3](#0-2) [4](#0-3) 

So if an attacker (as signer) submits N separate `TokenDiff` intents each with `delta = -1` on the same Nep245/Imt `TokenId`, paired with N intents from a counterpart account (which can be a second account the attacker controls) each with `delta = +1` on that same token, then:
- Each of the 2N intents independently calls `token_fee(..., amount=1, ...)` → `Pips::ZERO`, so `fees_collected` is empty for every intent — `internal_add_balance` to the fee collector is never invoked: [5](#0-4) 
- But `TransferMatcher` accumulates the withdrawals (sum = N) and deposits (sum = N) for that token across the whole batch, and `finalize_into` emits a **single real transfer of amount N** from attacker to counterpart: [6](#0-5) 

No cross-intent or batch-level aggregation feeds back into the fee computation — `token_fee`'s `amount` parameter is always the single intent's own `delta.unsigned_abs()`, never the actual net transfer size determined later by `TransferMatcher`. The invariant check (`InvariantViolated::UnmatchedDeltas`) only enforces that withdrawals and deposits net to zero per token across the batch — it does not touch fee correctness — confirmed by `execute_signed_intents`/`finalize`: [7](#0-6) 

This reproduces exactly the scenario validated by the existing `invariant_violated` test, which shows the batch-level netting is strict (a mismatch of 1 unit reverts everything), but says nothing about fee correctness per unit leg: [8](#0-7) 

No existing guard (`checked_*` arithmetic, `TransferMatcher::finalize`, `assert_one_yocto`, access-control) prevents this because the fee-exemption threshold (`amount<=1`) was designed for genuinely atomic NFT/MT unit transfers, not to be evaluated per-leg of an artificially chunked bulk transfer.

### Title
Nep245/Imt protocol fee bypass via unit-sized `TokenDiff` leg splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` tokens whenever a single intent's leg has `|delta| <= 1`, but this check is evaluated per-intent, not against the actual aggregate amount that `TransferMatcher::finalize` nets and transfers across the whole batch. An attacker can split any bulk Nep245/Imt transfer into N intents of `delta = ±1` each, causing zero fee to be collected while the real transferred volume is N.

### Finding Description
The broken binding is: `fees_collected[T]` should equal `Pips::fee_ceil` of the true amount moved for token `T` in the batch, but it is instead the sum of `Pips::fee_ceil` evaluated on each individual leg's own `|delta|` in isolation. Because `Nep245`/`Imt` tokens are fee-exempt when `amount <= 1` (intended for indivisible unit transfers), an attacker signs N `TokenDiff` intents debiting `-1` from their own account and pairs them (in the same `execute_intents`/`MultiPayload` batch) with N `TokenDiff` intents from a second account they control crediting `+1`. Each of the 2N intents is fee-exempt (`amount == 1`), so `fees_collected` stays empty for every intent and nothing is ever added to the fee collector's balance. However, `internal_sub_balance`/`internal_add_balance` accumulate these unit deltas into `TransferMatcher`, which sums them per account/token before `finalize_into` emits one real transfer of size N between the two accounts. A single `TokenDiff` intent moving `delta = -N` directly would have paid `Pips::fee_ceil(N)` to the fee collector via the exact same closure mechanism used in existing tests. Splitting the same economic transfer into unit legs therefore fully bypasses the fee with no code path checking the aggregate.

### Impact Explanation
Protocol fees are systematically bypassed for any Nep245 (multi-token) or Imt asset, for arbitrary amounts, at the cost of submitting more intents in the batch. This is a direct "protocol fees bypassed" Critical-category impact: the fee collector configured by the DAO is permanently deprived of revenue on all such assets, and the technique is fully repeatable across accounts, tokens, and batches — it requires no privileged role, only two accounts under the attacker's control (or collusion with any counterparty).

### Likelihood Explanation
Preconditions: attacker needs a Nep245/Imt balance and a second account (or willing counterparty) to receive it, and a nonzero configured fee. No special role or relayer key is needed — just standard `execute_intents`/`MultiPayload` signing capability, well within an unprivileged signer's reach. The only cost is transaction/gas overhead for many small intents in one batch, which is economically negligible relative to the fee saved on large transfers, making this highly feasible and repeatable.

### Recommendation
Compute Nep245/Imt fee exemption against the *net* per-token delta actually applied to the signer's account across the whole intent batch (or against the amount `TransferMatcher` ultimately resolves), not against each individual `TokenDiff` leg's own magnitude. Alternatively, aggregate per-token deltas across all `TokenDiff` intents from the same execution before evaluating `token_fee`.

### Proof of Concept
`cargo test` (near-workspaces sandbox) plan:
1. Deploy defuse contract with `fee = Pips::ONE_PERCENT` (or any nonzero fee) and a fee collector account.
2. Create attacker account A and a second account B (both controlled by the tester), deposit `k = 1000` units of a Nep245/Imt asset to A.
3. Build a `MultiPayload` batch containing 1000 `TokenDiff` intents signed by A, each `diff = {token: -1}`, and 1000 `TokenDiff` intents signed by B, each `diff = {token: +1}`.
4. Call `execute_intents` with this batch; assert it succeeds (no `InvariantViolated`).
5. Assert `fee_collector`'s balance for that token is `0`.
6. In a separate run, submit a single `TokenDiff` from A (`delta = -1000`) matched by a single `TokenDiff` from B (`delta = TokenDiff::closure_delta(token, 1000, fee)`), and assert `fee_collector`'s balance equals `Pips::ONE_PERCENT.fee_ceil(1000)` (nonzero).
7. Compare: same aggregate 1000-unit transfer, `0` fee vs nonzero fee, demonstrating the bypass.

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L295-333)
```rust
impl TokenTransferMatcher {
    #[inline]
    pub fn deposit(&mut self, owner_id: AccountId, amount: u128) -> bool {
        Self::sub_add(&mut self.withdrawals, &mut self.deposits, owner_id, amount)
    }

    #[inline]
    pub fn withdraw(&mut self, owner_id: AccountId, amount: u128) -> bool {
        Self::sub_add(&mut self.deposits, &mut self.withdrawals, owner_id, amount)
    }

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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-118)
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

    #[inline]
    fn verify_intent_nonce(&self, nonce: Nonce, intent_deadline: Timestamp) -> Result<()> {
        let Some(nonce) = VersionedNonce::maybe_from(nonce) else {
            return Ok(());
        };

        match nonce {
            VersionedNonce::V1(SaltedNonce {
                salt,
                nonce: ExpirableNonce { deadline, .. },
            }) => {
                if !self.state.is_valid_salt(salt) {
                    return Err(DefuseError::InvalidSalt);
                }

                if intent_deadline > deadline {
                    return Err(DefuseError::DeadlineGreaterThanNonce);
                }

                if deadline < Timestamp::now() {
                    return Err(DefuseError::NonceExpired);
                }
            }
        }

        Ok(())
    }

    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L277-349)
```rust
#[rstest]
#[tokio::test]
async fn invariant_violated(#[future(awt)] env: Env) {
    let (user1, user2, ft1, ft2) = futures::join!(
        env.create_user(),
        env.create_user(),
        env.create_token(),
        env.create_token(),
    );

    let ft1_token_id = TokenId::from(Nep141TokenId::new(ft1.contract_id().clone()));
    let ft2_token_id = TokenId::from(Nep141TokenId::new(ft2.contract_id().clone()));

    env.initial_ft_storage_deposit(
        vec![user1.account_id(), user2.account_id()],
        vec![ft1.contract_id(), ft2.contract_id()],
    )
    .await;

    // deposit
    futures::try_join!(
        env.defuse_ft_deposit_to(ft1.contract_id(), 1000, user1.account_id(), None),
        env.defuse_ft_deposit_to(ft2.contract_id(), 2000, user2.account_id(), None)
    )
    .expect("Failed to deposit tokens");

    let signed = try_join_all([
        user1.sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), -1000),
                        (ft2_token_id.clone(), 2000),
                    ])
                    .unwrap(),
                memo: None,
                referral: None,
            }],
        ),
        user1.sign_defuse_payload_default(
            &env.defuse,
            [TokenDiff {
                diff: TokenDeltas::default()
                    .with_apply_deltas([
                        (ft1_token_id.clone(), 1000),
                        (ft2_token_id.clone(), -1999),
                    ])
                    .unwrap(),
                memo: None,
                referral: None,
            }],
        ),
    ])
    .await
    .unwrap();

    assert_eq!(
        env.defuse
            .simulate_intents(MultiPayloadArgs { signed: &signed })
            .await
            .unwrap()
            .invariant_violated
            .unwrap()
            .into_unmatched_deltas(),
        Some(TokenDeltas::new(
            std::iter::once((ft2_token_id.clone(), 1)).collect()
        ))
    );

    env.defuse_execute_intents(env.defuse.contract_id(), signed)
        .await
        .unwrap_err();
```
