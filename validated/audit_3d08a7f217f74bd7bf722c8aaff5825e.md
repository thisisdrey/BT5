### Title
Batch-order dependent key revocation lets an already-signed stale intent execute after the signer's key removal is bypassed by processing order - ([File: contracts/defuse/core/src/engine/mod.rs])

### Summary
`execute_signed_intent` checks `has_public_key(signer_id, public_key)` against the *current* cumulative state at the moment each `MultiPayload` item is processed, not against the state at the moment that item was signed. Since `execute_intents`/`simulate_intents` accept `Vec<MultiPayload>` from any unprivileged caller and process items strictly in the given order, an attacker can place an already-signed, stale intent (signed with key `K1`) before an independently-signed `RemovePublicKey(K1)` intent in the same batch, causing the stale intent to execute even though the signer intended the removal to invalidate `K1` for any pending/unsubmitted signed messages.

### Finding Description
The intended binding is: `has_public_key(signer_id, K1)` evaluated when processing item *i* should reflect the key state the signer *believed* was in effect when authorizing execution of that item, i.e., revocation via `RemovePublicKey` should invalidate any signed-but-not-yet-executed messages under `K1`. Instead, the actual binding implemented is: `has_public_key(signer_id, K1)` at processing time == the *live, sequentially mutated* state of the batch up to that point, per [1](#0-0) .

`execute_signed_intents` iterates the caller-supplied `Vec<MultiPayload>` in order and calls `execute_signed_intent` for each item sequentially, mutating `self.state` (including public keys) as it goes: [2](#0-1) . Each item independently verifies its own NEP-413 signature, checks `has_public_key` against the mutated state at that point, commits its own nonce, and then executes its intents: [3](#0-2) .

`RemovePublicKey` is itself just another `Intent` variant executed via `engine.state.remove_public_key(...)`, requiring its own independently signed `MultiPayload` (e.g. signed by a different, primary key `K2`): [4](#0-3) . `has_public_key` itself is a simple point-in-time lookup with no history/versioning: [5](#0-4) .

The `execute_intents` entrypoint is callable by anyone and takes the batch order verbatim from the caller with no reordering protection or "revocation must be applied first" rule: [6](#0-5) . Nonce checks (`verify_intent_nonce`/`commit_nonce`) only prevent replay of the *same* signed payload; they do nothing to bind a payload's validity to the key state at signing time, so they don't prevent this reordering attack: [7](#0-6) .

Exploit flow:
1. Signer previously signs intent A (e.g., an `FtWithdraw`/`Transfer`) using session/rotatable key `K1`, and this payload is held by (or leaked to) an unprivileged relayer/attacker who has not yet submitted it.
2. Signer later decides to revoke `K1` (e.g., session ended, suspected leak) and signs intent B: `RemovePublicKey{ public_key: K1 }` with their primary key `K2`, intending that submitting B invalidates any use of `K1`, including A.
3. Attacker (an unprivileged relayer, since `execute_intents` accepts any `MultiPayload` batch from any caller) submits `execute_intents([A, B])` instead of `[B]` alone or `[B, A]`.
4. At processing of A, `has_public_key(signer, K1)` is still `true` (B hasn't executed yet), so A's `has_public_key` check passes, A's nonce commits, and A executes (funds move). Only afterward does B remove `K1`.
5. The signer's intended revocation semantics are defeated: `K1`, "revoked" from the signer's point of view, authorized one more real fund movement chosen by the attacker/relayer, not the signer.

This is not key-compromise or DAO/role-based; it requires only ordinary unprivileged relayer capability to control batch composition/order, consistent with the allowed threat model.

### Impact Explanation
Funds move under authorization the signer intended to have revoked before submission, i.e., a stale (but validly signed) transfer/withdraw intent executes after the signer already signed a key-removal intent meant to void it. This matches the Critical category: "tokens moved, credited or withdrawn without the owner's valid signature or authorisation" in the sense that owner's *current* authorization state (post-revocation) is bypassed by exploiting a race the owner cannot control, only the batch submitter can. This is repeatable for any signer who uses rotatable/session keys and any prior stale signed intent (Transfer, FtWithdraw, NftWithdraw, MtWithdraw, NativeWithdraw, TokenDiff, etc.) that has not yet been executed at the time of revocation.

### Likelihood Explanation
Preconditions: the signer must have used a non-implicit, removable public key (`K1`) for signing at least one previously-signed-but-unexecuted intent, and later sign a `RemovePublicKey(K1)` intent with a different key. The "stale intent" must be known to/held by an unprivileged relayer/attacker who is also the one submitting the batch to `execute_intents` (any address can call this). No special role or privilege is required, and the attack costs only ordinary gas for calling `execute_intents`. This is a realistic pattern for systems using ephemeral/session keys where users pre-sign multiple intents and rely on key removal to invalidate unused ones.

### Recommendation
Bind each item's key-validity/authorization check to a snapshot taken before any mutation from *other* items in the same batch is applied — e.g., either (a) process all `RemovePublicKey`/`AddPublicKey` intents in a first pass across the whole batch before any other intent is evaluated with `has_public_key`, or (b) require that `has_public_key` checks are evaluated against the state as of the start of the top-level `execute_signed_intents` call rather than the mutated intermediate state, or (c) disallow mixing key-management intents with other intents from other independently signed payloads in the same call, and/or require the caller to be the account itself (`predecessor_id == signer_id`) for batches containing `RemovePublicKey`.

### Proof of Concept
```rust
// cargo test in contracts/defuse (or tests/ sandbox), pseudo-outline:

#[tokio::test]
async fn stale_intent_survives_key_removal_via_reorder() {
    let env = env().await;
    let user = env.create_user().await;
    let k1 = generate_keypair(); // session key

    // 1. add K1 to user's account
    let add_k1 = user.sign_defuse_payload_default(&env.defuse, [AddPublicKey{ public_key: k1.public() }]).await.unwrap();
    env.execute_intents([add_k1]).await.unwrap();

    // 2. sign a stale transfer intent with K1 (held by attacker/relayer)
    let stale_transfer = user.sign_defuse_payload_with_key(&env.defuse, &k1, [Transfer { /* moves funds */ }]).await.unwrap();

    // 3. user signs removal of K1 with their primary key, intending to void `stale_transfer`
    let remove_k1 = user.sign_defuse_payload_default(&env.defuse, [RemovePublicKey{ public_key: k1.public() }]).await.unwrap();

    // 4. attacker (unprivileged relayer) submits batch in the WRONG order
    let result = env.execute_intents([stale_transfer.clone(), remove_k1.clone()]).await;

    // ASSERT: binding violated - stale_transfer executed despite K1 removal intent being co-signed/submitted
    assert!(result.is_ok(), "stale transfer executed");
    assert!(!env.defuse.has_public_key(user.account_id(), k1.public()).await); // K1 now removed
    // yet funds already moved by stale_transfer before removal took effect
    assert_funds_moved(&env, &user).await;
}
```
Both sides of the binding to check: `has_public_key(signer, K1)` at the time `stale_transfer` is *signed* (true, key present) vs. at the time `stale_transfer` is *processed* in the `[stale_transfer, remove_k1]` batch (also true, because removal hasn't executed yet) — showing that the check is satisfied by stale, no-longer-intended-to-be-valid state, and diverges from the outcome the signer expected (`[remove_k1]` alone, or `[remove_k1, stale_transfer]`, would reject `stale_transfer`).

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L32-40)
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
```

**File:** contracts/defuse/core/src/engine/mod.rs (L42-82)
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
```

**File:** contracts/defuse/core/src/intents/account.rs (L65-93)
```rust
impl ExecutableIntent for RemovePublicKey {
    #[inline]
    fn execute_intent<S, I>(
        self,
        signer_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> crate::Result<()>
    where
        S: State,
        I: Inspector,
    {
        engine
            .state
            .remove_public_key(signer_id.to_owned(), self.public_key)?;
        engine
            .inspector
            .on_event(DefuseEvent::PublicKeyRemoved(MaybeIntentEvent::new_intent(
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

**File:** contracts/defuse/src/contract/intents/mod.rs (L26-42)
```rust
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
```
