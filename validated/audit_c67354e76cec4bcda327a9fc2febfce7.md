This confirms the critical architectural fact needed to evaluate the claim: `execute_intents` (`contracts/defuse/src/contract/intents/mod.rs:27`) calls `Engine::new(self, ...).execute_signed_intents(signed)` synchronously, which loops over every `MultiPayload` via `execute_signed_intent` and only calls `finalize()` once **after** the entire loop completes.This confirms the key architectural point: `DefuseIntents::execute_intent` (`contracts/defuse/core/src/intents/mod.rs:97-113`) is a synchronous `for` loop over all `Intent`s in the batch — there is no yield point, no cross-contract call awaited, and no Promise execution interleaved within this loop. `notify_on_transfer` only schedules a `Promise`/`.detach()`-ed future receipt; it doesn't block or execute synchronously.

### Title
No vulnerability found — `NotifyOnTransfer.state_init` reentrancy cannot interleave with pending intents in the same batch - (File: `contracts/defuse/core/src/intents/mod.rs`, `contracts/defuse/core/src/engine/mod.rs`, `contracts/defuse/src/contract/tokens/nep245/core.rs`)

### Summary
The claimed binding violation requires the attacker's re-entrant `execute_intents` call (triggered from the deployed contract's `mt_on_transfer`) to execute *before* the remaining intents of the original signer's `DefuseIntents` batch. This is architecturally impossible on NEAR: intent execution within a single `execute_intents` call is fully synchronous Rust computation with no yield points, while the `state_init` + `mt_on_transfer` notification is only *scheduled* as a `Promise` that executes in a later, separate receipt after the entire outer transaction — including `finalize()` — has already committed.

### Finding Description
The binding to check: `signer_id_batch_i.intents[j].effect_time <= finalize_batch.commit_time` for all `j`, versus the claim that `mt_on_transfer`'s re-entrant `execute_intents(...)` call executes at some `commit_time' < finalize_batch.commit_time`.

Tracing the actual path:
1. `execute_intents` (`contracts/defuse/src/contract/intents/mod.rs:27`) calls `Engine::new(self, ...).execute_signed_intents(signed)`.
2. `execute_signed_intents` (`contracts/defuse/core/src/engine/mod.rs:32-40`) loops synchronously over every `MultiPayload`, calling `execute_signed_intent` for each, and only calls `self.finalize()` once, after the loop, at line 39.
3. `execute_signed_intent` (`contracts/defuse/core/src/engine/mod.rs:42-83`) verifies the signature/nonce/public key and then calls `intents.execute_intent(&signer_id, self, hash)` — this is `DefuseIntents::execute_intent` (`contracts/defuse/core/src/intents/mod.rs:97-113`), a plain synchronous `for intent in self.intents { intent.execute_intent(...)?; }` loop with **no yield points**.
4. Within that loop, `Transfer::execute_intent` (`contracts/defuse/core/src/intents/tokens.rs:82-128`) calls `internal_sub_balance`/`internal_add_balance` in-memory, then calls `engine.state.notify_on_transfer(...)` which (at the contract layer, `contracts/defuse/src/contract/intents/state.rs:243-263`) builds a `Promise` via `notify_and_resolve_transfer(...).detach()`. Building a `Promise` in `near-sdk` does **not** execute it — it only schedules a receipt to be dispatched by the NEAR runtime *after* the current function call (and its state mutations) return successfully.
5. `Promise::state_init(...)` (`contracts/defuse/src/contract/tokens/nep245/core.rs:281-306`) similarly only appends an action to that already-scheduled, not-yet-executed `Promise`.
6. Therefore, deployment of the deterministic account and invocation of its `mt_on_transfer` (and any subsequent re-entrant call to `execute_intents`) happens in a receipt processed strictly after the current receipt — the one containing the entire original `DefuseIntents` batch and its `finalize()` call — has fully committed to state.

Since all remaining intents in the same `DefuseIntents` (e.g., subsequent `Transfer`, `TokenDiff`, `FtWithdraw` intents signed by the original signer) are processed by the same synchronous loop *before* `finalize()` runs, and the re-entrant `mt_on_transfer → execute_intents` call can only occur in a later receipt *after* that `finalize()` has already committed, there is no way for the attacker's re-entrant call to affect, race, or precede the original signer's remaining intents. The premise of the audit prompt — that reentry happens "before the outer `Transfer`'s intent-level accounting... execute[s]" — does not hold given NEAR's actor-model receipt scheduling and this codebase's synchronous intent-execution loop.

The newly-deployed receiver account only gains authority over its own just-credited balance (added via `internal_add_balance(receiver_id, N)`), which is legitimately owned by that account after the `Transfer` intent completes; it has no `public_key` binding to the original signer's `AccountId`, so `has_public_key(&signer_id, &public_key)` (`contracts/defuse/core/src/engine/mod.rs:71-73`) would reject any attempt by the deployed contract to sign on behalf of the original signer even if it somehow tried in a later receipt.

### Impact Explanation
None. No value moves without the owner's authorization, no batch fails to net to zero, and no signer's remaining intents are affected by the reentrant call, because the reentrant call cannot execute concurrently with or before the remainder of the batch under NEAR's execution model.

### Likelihood Explanation
N/A — the described interleaving is not reachable.

### Recommendation
No fix needed for this specific claim. (General best practice already followed: `finalize()` runs once synchronously before any scheduled `Promise` executes, and `NearToken::ZERO` deposit on `state_init` prevents unauthorized value transfer during deployment.)

### Proof of Concept
Not applicable — no divergence to demonstrate. A `near-workspaces`/sandbox test would show that by the time the deployed receiver's `mt_on_transfer` (and any nested `execute_intents` call) executes, the original transaction's outcome (including all sibling intents in the same `DefuseIntents`) is already finalized on-chain, matching ` [1](#0-0) ` `execute_signed_intents` semantics. [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L32-83)
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

**File:** contracts/defuse/core/src/intents/tokens.rs (L107-128)
```rust
        engine
            .state
            .internal_sub_balance(sender_id, self.tokens.clone())?;
        engine
            .state
            .internal_add_balance(self.receiver_id.clone(), self.tokens.clone())?;

        if let Some(mut notification) = self.notification {
            notification.min_gas = Some(
                notification
                    .min_gas
                    .unwrap_or(MT_ON_TRANSFER_GAS_DEFAULT)
                    .max(MT_ON_TRANSFER_GAS_MIN),
            );

            engine
                .state
                .notify_on_transfer(sender_id, self.receiver_id, self.tokens, notification);
        }

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep245/core.rs (L273-308)
```rust
    pub(crate) fn notify_on_transfer(
        sender_id: AccountId,
        previous_owner_ids: Vec<AccountId>,
        receiver_id: AccountId,
        token_ids: Vec<defuse_nep245::TokenId>,
        amounts: Vec<U128>,
        notify: NotifyOnTransfer,
    ) -> Promise {
        let mut p = Promise::new(receiver_id);

        if let Some(state_init) = notify.state_init {
            // No need to require `receiver_id == state_init.derive_account_id()` here,
            // since Near runtime does this validation for us and current receipt will
            // fail in case of mismatch anyway:
            // https://github.com/near/nearcore/blob/523c659ac47ea31205fec830a1427a71352c605a/runtime/runtime/src/verifier.rs#L637-L644

            p = p.state_init(
                state_init,
                // we can't spend native NEAR from sender's account during the deposits
                NearToken::ZERO,
            );
        }

        ext_mt_receiver::ext_on(p)
            .with_static_gas(notify.min_gas.unwrap_or_default())
            // distribute remaining gas here
            .with_unused_gas_weight(1)
            .mt_on_transfer(
                sender_id,
                previous_owner_ids,
                token_ids,
                amounts,
                notify.msg,
            )
    }

```

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-42)
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
```
