## No vulnerability found for this question.

The premise in the "Proof idea" already concedes the outcome: `signed.verify()` cryptographically validates that the `MultiPayload` signature was produced by the private key corresponding to the returned `PublicKey`, before `execute_signed_intent` ever consults `has_public_key`. [1](#0-0) 

The check at line 71, `self.state.has_public_key(&signer_id, &public_key)`, only asks "is `public_key` registered under `signer_id`" — it does not re-verify signature validity, but it doesn't need to, because `public_key` at that point is not attacker-supplied data; it is the output of `signed.verify()`, which already required possession of the matching private key. [2](#0-1) [3](#0-2) 

For the attack to work as described, the attacker would need to submit a `WebAuthn` payload whose signature verifies as `P` while claiming `signer_id = A` (the victim), without holding `P`'s private key. That is exactly what `signed.verify()` prevents — an invalid/forged signature for `P` fails verification and `execute_signed_intent` returns `DefuseError::InvalidSignature` before `has_public_key` is even reached.

Additionally, `add_public_key` can only be invoked by the account's own authorized signer: either via a direct contract call gated by `ensure_auth_predecessor_id` (predecessor must equal the account, requiring `assert_one_yocto` and predecessor-based auth), or via an `AddPublicKey` intent that itself must be signed by an already-registered key of `signer_id`. [4](#0-3) [5](#0-4) 

So a key `P` can only ever become registered to two accounts (A and B) if the actual private-key holder of `P` deliberately authorizes `add_public_key(P)` under both accounts — an action requiring `P`'s private key each time, not something an unprivileged attacker lacking that key can trigger. Since the attacker cannot forge a signature verifying as `P` without holding `P`'s private key, the claimed binding break — authenticating as `signer_id = A` using `public_key = P` that the attacker doesn't control — cannot be realized. `has_public_key` alone being "membership-only" is irrelevant here because it is never reached with an attacker-controlled `public_key` that wasn't already cryptographically proven via `verify()`.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L42-73)
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
```

**File:** contracts/defuse/src/contract/intents/state.rs (L42-50)
```rust
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

**File:** contracts/defuse/src/contract/accounts/account/mod.rs (L86-90)
```rust
    #[inline]
    pub fn has_public_key(&self, me: &AccountIdRef, public_key: &PublicKey) -> bool {
        !self.is_implicit_public_key_removed() && me == public_key.to_implicit_account_id()
            || self.public_keys.contains(public_key)
    }
```

**File:** contracts/defuse/src/contract/accounts/mod.rs (L40-46)
```rust
    #[payable]
    fn add_public_key(&mut self, public_key: PublicKey) {
        assert_one_yocto();
        let account_id = self.ensure_auth_predecessor_id();

        self.add_public_key_and_emit_event(account_id.as_ref(), public_key);
    }
```

**File:** contracts/defuse/core/src/intents/account.rs (L27-55)
```rust
impl ExecutableIntent for AddPublicKey {
    #[inline]
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
        engine
            .state
            .add_public_key(signer_id.to_owned(), self.public_key)?;

        engine
            .inspector
            .on_event(DefuseEvent::PublicKeyAdded(MaybeIntentEvent::new_intent(
                AccountEvent::new(
                    Cow::Borrowed(signer_id),
                    PublicKeyEvent {
                        public_key: Cow::Borrowed(&self.public_key),
                    },
                ),
                intent_hash,
            )));
        Ok(())
    }
```
