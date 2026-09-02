## No vulnerability found for this question.

**Reasoning:**

The claimed exploit does not work because fee amounts collected via `internal_add_balance` are also recorded in the batch-wide `TransferMatcher`, which enforces that all deposits and withdrawals across the entire `MultiPayload` net to zero before any balance change is finalized.

Tracing the scenario: signer submits `{T: +X}` then `{T: -X}` in the same `MultiPayload`.

- Intent 1 (`+X`): `TokenDiff::execute_intent` calls `internal_apply_deltas` → `internal_add_balance`, which both credits the signer's real balance and records `deltas.deposit(signer, T, X)` in the `TransferMatcher`. [1](#0-0) 

- Intent 2 (`-X`): since `delta < 0`, a fee is computed and added to `fees_collected`, then deposited to the fee collector via `internal_add_balance` — which *also* records a `deltas.deposit(fee_collector, T, fee)` entry. [2](#0-1) 

So across the whole batch, for token `T`: total deposits = `X` (intent 1) + `fee` (fee collector) = `X + fee`; total withdrawals = `X` (intent 2, via `internal_sub_balance`/`deltas.withdraw`). These do not balance whenever `fee > 0`.

`Engine::execute_signed_intents` always calls `self.finalize()` at the end, which invokes `TransferMatcher::finalize()`. Any non-zero leftover after matching deposits against withdrawals returns `InvariantViolated::UnmatchedDeltas`, causing the whole `execute_intents` call to fail. [3](#0-2) [4](#0-3) 

The repository's own test `invariant_violated` demonstrates exactly this class of scenario: mismatched deltas (including the fee-induced imbalance) cause `execute_intents` to return an error, and it explicitly asserts balances remain unchanged afterward — i.e., the whole transaction reverts, not just the offending leg. [5](#0-4) 

Therefore either `fee == 0` (no fee to over-collect, net delta correctly stays zero), or `fee > 0` and the batch is rejected entirely with no state change — there is no path where `fees_collected` for the signer ends up non-zero while their true net delta is zero. The binding claimed to be broken (`fees_collected == fee on true net delta`) is actually enforced indirectly by the invariant check requiring the fee-inclusive deposit/withdrawal ledger to balance across the whole batch.

### Citations

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-284)
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
}
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-101)
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

        engine.inspector.on_event(DefuseEvent::TokenDiff(
            [MaybeIntentEvent::new_intent(
                AccountEvent::new(
                    signer_id,
                    TokenDiffEvent {
                        diff: Cow::Borrowed(&self),
                        fees_collected: fees_collected.clone(),
                    },
                ),
                intent_hash,
            )]
            .as_slice()
            .into(),
        ));

        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
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

**File:** tests/src/tests/defuse/intents/token_diff.rs (L303-372)
```rust
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

    // balances should stay the same
    assert_eq!(
        env.contract::<Mt>(env.defuse.contract_id())
            .mt_batch_balance_of(MtBatchBalanceOfArgs {
                account_id: user1.account_id(),
                token_ids: &[ft1_token_id.to_string(), ft2_token_id.to_string()],
            })
            .await
            .unwrap(),
        [U128(1000), U128(0)]
    );

    assert_eq!(
        env.contract::<Mt>(env.defuse.contract_id())
            .mt_batch_balance_of(MtBatchBalanceOfArgs {
                account_id: user2.account_id(),
                token_ids: &[ft1_token_id.to_string(), ft2_token_id.to_string()],
            })
            .await
            .unwrap(),
        [U128(0), U128(2000)]
    );
```
