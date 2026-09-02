No vulnerability found for this question.

**Analysis:** The claimed exploit path does not produce a partial-persistence bug because NEAR's execution model guarantees atomicity within a single contract call.

`TokenDiff::execute_intent` does call `internal_apply_deltas` on the signer and then `internal_add_balance(fee_collector, fees_collected)` before the batch-level check [1](#0-0) , and `Engine::execute_signed_intents` only calls `self.finalize()` (which invokes `Deltas::finalize` / `TransferMatcher`) after all intents in the batch have executed [2](#0-1) . However, `execute_intents` wraps the whole engine call in `.unwrap_or_else(|e| e.panic())` [3](#0-2) . A panic inside a NEAR contract call discards **all** state mutations made during that receipt — the runtime never commits the trie changes for a function call that ends in panic, regardless of how many intermediate mutations (`internal_add_balance`, `internal_sub_balance`) occurred before the panic. This is a base guarantee of the NEAR runtime, not something the contract needs to implement itself.

This is exactly verified by the existing test `invariant_violated` in the repo: it submits an unmatched batch (deltas differ by 1 unit on `ft2`), asserts `simulate_intents` reports `InvariantViolated::UnmatchedDeltas`, then calls `env.defuse_execute_intents(...).await.unwrap_err()`, and finally asserts that **both** `user1` and `user2` balances for `ft1`/`ft2` are unchanged from their pre-call deposits [4](#0-3) . This directly demonstrates that fee-collector credits and signer debits made during `TokenDiff::execute_intent` are rolled back together when `finalize()` fails, exactly as the CONSERVATION binding requires.

Since the transaction reverts atomically on panic (the whole receipt, including any `internal_add_balance` to `fee_collector`), there is no code path where `fee_collector`'s credit persists while the signer's negative delta is lost — the equality `sum(token_balances changes for T across the call) == 0` holds trivially because either everything commits (finalize succeeds) or nothing commits (panic). No partial persistence is possible.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L64-101)
```rust
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

**File:** contracts/defuse/src/contract/intents/mod.rs (L27-30)
```rust
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L277-373)
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
}
```
