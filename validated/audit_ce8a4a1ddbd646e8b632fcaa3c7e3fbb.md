No vulnerability found for this question.

**Rationale:**

The claimed binding — "(signer_id, public_key) authorised == pair explicitly registered via `add_public_key`" — does not match the actual invariant implemented in this codebase. The real invariant is: an implicit account's key derived from its own `AccountId` is trusted by default unless explicitly removed via `remove_public_key`, mirroring NEAR's native implicit-account semantics. This is visible in the non-locked fallback at [1](#0-0)  and is the exact same default applied when an `Account` entry is first created via `Account::new`, where `flags` are initialized based on `has_implicit_public_key(me)` [2](#0-1) , and then consulted via `Account::has_public_key` [3](#0-2) . So an implicit account's own derived key is *always* considered "registered" by design, whether or not an account entry exists yet — this is intended, documented behavior, not a bypass.

Critically, even granting the premise, `has_public_key` is a pure read (`StateView`) that ignores lock state by design, since read/view operations return underlying values regardless of lock [4](#0-3) . Actual authorisation to *execute* an intent additionally requires `commit_nonce`, which explicitly checks lock state and rejects mutation with `DefuseError::AccountLocked` when the account is locked [5](#0-4) , called right after the `has_public_key` check inside `Engine::execute_signed_intent` [6](#0-5) . This is confirmed by an existing test that locks an account and asserts that any signed intent execution attempt fails with `AccountLocked`, even for a valid registered key [7](#0-6) .

Therefore, while `has_public_key` may return `true` via the implicit fallback for a locked account (matching intended default semantics), this has no exploitable effect: no balance can move, no intent executes, and no fund transfer bypasses lock enforcement. The proof idea in the question only demonstrates a read-only query result, not any state mutation or value transfer, so it does not meet the required Critical/High impact bar (value leaving the Verifier without the owner's authorisation, or an intent executed against a locked account).

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L41-50)
```rust
    #[inline]
    fn has_public_key(&self, account_id: &AccountIdRef, public_key: &PublicKey) -> bool {
        self.accounts
            .get(account_id)
            .map(Lock::as_inner_unchecked)
            .map_or_else(
                || account_id == public_key.to_implicit_account_id(),
                |account| account.has_public_key(account_id, public_key),
            )
    }
```

**File:** contracts/defuse/src/contract/accounts/account/mod.rs (L47-60)
```rust
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

**File:** contracts/defuse/src/contract/accounts/account/mod.rs (L86-90)
```rust
    #[inline]
    pub fn has_public_key(&self, me: &AccountIdRef, public_key: &PublicKey) -> bool {
        !self.is_implicit_public_key_removed() && me == public_key.to_implicit_account_id()
            || self.public_keys.contains(public_key)
    }
```

**File:** contracts/defuse/core/src/lock.rs (L55-58)
```rust
    #[inline]
    pub const fn as_inner_unchecked(&self) -> &T {
        &self.value
    }
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L172-184)
```rust
    fn commit_nonce(&mut self, account_id: AccountId, nonce: Nonce) -> Result<()> {
        if self.view.is_nonce_used(&account_id, nonce) {
            return Err(DefuseError::NonceUsed);
        }

        self.accounts
            .get_or_create(account_id.clone(), |account_id| {
                self.view.is_account_locked(account_id)
            })
            .get_mut()
            .ok_or(DefuseError::AccountLocked(account_id))?
            .commit_nonce(nonce)
    }
```

**File:** contracts/defuse/core/src/engine/mod.rs (L70-77)
```rust
        // make sure the account has this public key
        if !self.state.has_public_key(&signer_id, &public_key) {
            return Err(DefuseError::PublicKeyNotExist(signer_id, public_key));
        }

        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;
```

**File:** tests/src/tests/defuse/accounts/force.rs (L338-361)
```rust
    // try to execute intents on behalf of locked account
    {
        let locked_payload = locked_account
            .sign_defuse_payload_default(&env.defuse, Vec::<Intent>::new())
            .await
            .unwrap();
        let nonce = locked_payload.extract_nonce().unwrap();

        env.defuse_simulate_and_execute_intents(env.defuse.contract_id(), [locked_payload])
            .await
            .assert_err_contains(
                DefuseError::AccountLocked(locked_account.account_id().clone()).to_string(),
            );

        assert!(
            !env.defuse
                .is_nonce_used(IsNonceUsedArgs {
                    account_id: locked_account.account_id(),
                    nonce: &nonce,
                })
                .await
                .unwrap()
        );
    }
```
