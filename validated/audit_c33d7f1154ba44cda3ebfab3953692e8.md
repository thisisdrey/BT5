This confirms the premise of the question is false.

`simulate_intents` and `execute_intents` in `contracts/defuse/src/contract/intents/mod.rs` only call `Engine::execute_signed_intents`, which in `contracts/defuse/core/src/engine/mod.rs` verifies the payload, checks the public key, and calls `self.state.commit_nonce(signer_id.clone(), nonce)` — it never invokes `cleanup_nonce_by_prefix`/`cleanup_by_prefix` at all. [1](#0-0) [2](#0-1) 

The only entrypoint that reaches `cleanup_by_prefix` (via `Account::cleanup_nonce_by_prefix` → `State::cleanup_nonce_by_prefix`) is `GarbageCollector::cleanup_nonces`, which is gated by `#[access_control_any(roles(Role::DAO, Role::GarbageCollector))]` and requires `assert_one_yocto()`. [3](#0-2) [4](#0-3) [5](#0-4) 

Additionally, even when `cleanup_nonces` is called by an authorized `GarbageCollector`, the contract pre-filters via `is_nonce_cleanable`, which only allows cleanup when the specific nonce's deadline has passed or its salt is invalidated — the underlying `cleanup_by_prefix` (`BitMap256::cleanup_by_prefix`, which does `self.0.remove(&prefix)`) removes the *entire word* for that 248-bit prefix, but this call is only reached for a `(account_id, nonce)` pair explicitly supplied by the GarbageCollector, scoped to that specific account's storage map — an attacker's own committed nonces in a different account's storage cannot be planted into a victim account's nonce word, since each `Account`'s `MaybeLegacyAccountNonces` uses its own storage prefix (`prefix.as_slice().nest(AccountPrefix::OptimizedNonces)`), so account A's bits can never live in account B's map. [6](#0-5) [7](#0-6) [8](#0-7) 

The question's premise — that an unprivileged attacker can reach `cleanup_by_prefix` through `simulate_intents`/`execute_intents` — has no code path support. `cleanup_by_prefix` is only reachable through the access-controlled `cleanup_nonces` entrypoint, which the rules explicitly place out of scope for an "unprivileged attacker" (not a `Role::GarbageCollector` holder). There is also an explicit test, `legacy_nonces_cant_be_cleared`, and the broader `test_cleanup_nonces` test confirming non-expired nonces stay committed after cleanup attempts. [9](#0-8) [10](#0-9) 

#No vulnerability found for this question.

### Citations

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

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-64)
```rust
#[near]
impl Intents for Contract {
    #[pause(name = "intents")]
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
            .as_mt_event()
        {
            // NOTE: Not all `mt_transfer` events are refundable, but it's safe to check them
            // all at once since non-refundable transfers only increase the potential refund
            // log size without affecting correctness. This can actually prevent resolve transfer
            // from failing due to too long event log !!!
            event
                .check_refund()
                .unwrap_or_else(|err| err.panic())
                .emit();
        }
    }

    #[pause(name = "intents")]
    fn simulate_intents(&self, signed: Vec<MultiPayload>) -> SimulationOutput {
        let mut inspector = SimulateInspector::default();
        let engine = Engine::new(self.cached(), &mut inspector);

        let invariant_violated = match engine.execute_signed_intents(signed) {
            // do not log transfers
            Ok(_) => None,
            Err(DefuseError::InvariantViolated(v)) => Some(v),
            Err(err) => err.panic(),
        };

        SimulationOutput {
            report: inspector.into_report(),
            invariant_violated,
            state: StateOutput {
                fee: self.fee(),
                current_salt: self.salts.current(),
            },
        }
    }
```

**File:** contracts/defuse/src/contract/garbage_collector.rs (L9-27)
```rust
#[near]
impl GarbageCollector for Contract {
    #[access_control_any(roles(Role::DAO, Role::GarbageCollector))]
    #[payable]
    fn cleanup_nonces(&mut self, nonces: Vec<(AccountId, Vec<AsBase64<Nonce>>)>) {
        assert_one_yocto();

        for (account_id, nonces) in nonces {
            for nonce in nonces.into_iter().map(AsBase64::into_inner) {
                if !self.is_nonce_cleanable(nonce) {
                    continue;
                }

                // NOTE: all errors are omitted
                let [prefix @ .., _] = nonce;
                let _ = State::cleanup_nonce_by_prefix(self, &account_id, prefix);
            }
        }
    }
```

**File:** contracts/defuse/src/contract/garbage_collector.rs (L30-43)
```rust
impl Contract {
    #[inline]
    fn is_nonce_cleanable(&self, nonce: Nonce) -> bool {
        let Some(versioned_nonce) = VersionedNonce::maybe_from(nonce) else {
            return false;
        };

        match versioned_nonce {
            VersionedNonce::V1(SaltedNonce {
                salt,
                nonce: ExpirableNonce { deadline, .. },
            }) => deadline < Timestamp::now() || !self.is_valid_salt(salt),
        }
    }
```

**File:** contracts/defuse/src/contract/accounts/account/mod.rs (L41-60)
```rust
    pub fn new<S>(prefix: S, me: &AccountIdRef) -> Self
    where
        S: IntoStorageKey,
    {
        let prefix = prefix.into_storage_key();

        Self {
            nonces: MaybeLegacyAccountNonces::new(LookupMap::with_hasher(
                prefix.as_slice().nest(AccountPrefix::OptimizedNonces),
            )),
            flags: if has_implicit_public_key(me) {
                AccountFlags::empty()
            } else {
                AccountFlags::IMPLICIT_PUBLIC_KEY_REMOVED
            },
            public_keys: IterableSet::new(prefix.as_slice().nest(AccountPrefix::PublicKeys)),
            state: AccountState::new(prefix.as_slice().nest(AccountPrefix::State)),
            prefix,
        }
    }
```

**File:** contracts/defuse/src/contract/accounts/account/mod.rs (L111-117)
```rust
    /// Clears the all nonces with corresponding prefix if it was expired/invalidated.
    /// Returns whether the nonces was cleared,
    /// regardless of whether it was previously committed or not.
    #[inline]
    pub fn cleanup_nonce_by_prefix(&mut self, prefix: NoncePrefix) -> bool {
        self.nonces.cleanup_by_prefix(prefix)
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L132-145)
```rust
    #[inline]
    fn cleanup_nonce_by_prefix(
        &mut self,
        account_id: &AccountIdRef,
        prefix: NoncePrefix,
    ) -> Result<bool> {
        let account = self
            .accounts
            .get_mut(account_id)
            .ok_or_else(|| DefuseError::AccountNotFound(account_id.to_owned()))?
            .as_inner_unchecked_mut();

        Ok(account.cleanup_nonce_by_prefix(prefix))
    }
```

**File:** crates/bitmap/src/b256.rs (L48-51)
```rust
    #[inline]
    pub fn cleanup_by_prefix(&mut self, prefix: U248) -> bool {
        self.0.remove(&prefix).is_some()
    }
```

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L199-212)
```rust
    proptest! {
        #[test]
        fn legacy_nonces_cant_be_cleared(storage_prefix in storage_prefixes(), random_nonce : U256) {
            let legacy_nonces = get_legacy_map(&[random_nonce], storage_prefix.clone());
            let mut new = MaybeLegacyAccountNonces::with_legacy(
                legacy_nonces,
                LookupMap::with_hasher(storage_prefix),
            );

            let [prefix @ .., _] = random_nonce;
            assert!(!new.cleanup_by_prefix(prefix));
            assert!(new.is_used(random_nonce));
        }
    }
```

**File:** tests/src/tests/defuse/accounts/nonces.rs (L327-367)
```rust
    // skip if nonce is legacy / already cleared / is not expired / user does not exist
    {
        let unknown_user: AccountId = "unknown-user.near".parse().unwrap();

        user.defuse_cleanup_nonces(
            env.defuse.contract_id(),
            vec![
                (user.account_id().clone(), vec![expirable_nonce]),
                (user.account_id().clone(), vec![legacy_nonce]),
                (user.account_id().clone(), vec![long_term_expirable_nonce]),
                (unknown_user, vec![expirable_nonce]),
            ],
        )
        .await
        .unwrap();

        futures::join!(
            async {
                assert!(
                    env.defuse
                        .is_nonce_used(IsNonceUsedArgs {
                            account_id: user.account_id(),
                            nonce: &legacy_nonce,
                        })
                        .await
                        .unwrap()
                );
            },
            async {
                assert!(
                    env.defuse
                        .is_nonce_used(IsNonceUsedArgs {
                            account_id: user.account_id(),
                            nonce: &long_term_expirable_nonce,
                        })
                        .await
                        .unwrap()
                );
            }
        );
    }
```
