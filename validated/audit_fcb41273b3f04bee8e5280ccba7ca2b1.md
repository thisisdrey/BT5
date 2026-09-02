**No vulnerability found for this question.**

Analysis confirms the binding holds. The claim would require: `is_nonce_used(signer_id, nonce)` before call == `is_nonce_used(signer_id, nonce)` after a panicking `execute_intents` call (i.e., `false == false`, not `false == true`).

Tracing the code:

1. `Engine::execute_signed_intent` commits the nonce via `self.state.commit_nonce(signer_id.clone(), nonce)` before calling `intents.execute_intent(...)`, and if `execute_intent` fails, `execute_signed_intent` returns early — but this only matters within the single in-memory call. [1](#0-0) 

2. `commit_nonce` on `Deltas<S>` simply forwards to the underlying `S: State`, which for the top-level entrypoint is `Contract` itself. [2](#0-1) 

3. `Contract`'s `commit_nonce` implementation mutates the in-memory `self.accounts` map (a `LookupMap`/collection field on the `#[near(contract_state)]` struct), not directly writing to trie storage per-call. [3](#0-2) 

4. `execute_intents` is a single `&mut self` contract method. `Engine::new(self, ...)` wraps the same `&mut Contract`, so all balance and nonce mutations for the whole batch happen on one in-memory `Contract` instance during one function call. Only if `execute_signed_intents` returns `Ok` does execution proceed to emit events; if it returns `Err`, the call is terminated via `.unwrap_or_else(|e| e.panic())`. [4](#0-3) 

Under NEAR's runtime execution model, a contract's `#[near(contract_state)]` struct is only serialized and persisted to the trie by the SDK's generated wrapper *after* the exported function returns successfully. A panic aborts the entire receipt/function-call execution, and the runtime discards all in-memory changes made during that call — no partial writes to storage occur, and no borsh-serialized state update is flushed. This is standard, well-established NEAR contract semantics (not something this repo implements or could override), so a nonce committed via `commit_nonce` earlier in `execute_signed_intent`, within a batch that later triggers `Err(DefuseError::InvariantViolated(..))` from `TransferMatcher::finalize` (`contracts/defuse/core/src/engine/state/deltas.rs`, `finalize`/`finalize_into`), is never persisted, since the panic in `execute_intents` (`contracts/defuse/src/contract/intents/mod.rs:30`) reverts the whole call atomically. [5](#0-4) [6](#0-5) 

Therefore `is_nonce_used(signer_id, nonce)` before the panicking call equals `is_nonce_used(signer_id, nonce)` after it (`false == false`); the nonce is not burned, and there is no fund freezing or lockout. This matches the audit rule that requires the binding to actually diverge for a valid finding — it does not, since atomic revert-on-panic is guaranteed by the NEAR execution model itself, independent of any explicit rollback logic in this contract.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L75-82)
```rust
        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
```

**File:** contracts/defuse/core/src/engine/mod.rs (L113-118)
```rust
    #[inline]
    fn finalize(self) -> Result<Transfers> {
        self.state
            .finalize()
            .map_err(DefuseError::InvariantViolated)
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L122-125)
```rust
    #[inline]
    fn commit_nonce(&mut self, account_id: AccountId, nonce: Nonce) -> Result<()> {
        self.state.commit_nonce(account_id, nonce)
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L267-283)
```rust
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
```

**File:** contracts/defuse/src/contract/intents/state.rs (L123-130)
```rust
    #[inline]
    fn commit_nonce(&mut self, account_id: AccountId, nonce: Nonce) -> Result<()> {
        self.accounts
            .get_or_create(account_id.clone())
            .get_mut()
            .ok_or(DefuseError::AccountLocked(account_id))?
            .commit_nonce(nonce)
    }
```

**File:** contracts/defuse/src/contract/intents/mod.rs (L27-42)
```rust
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
