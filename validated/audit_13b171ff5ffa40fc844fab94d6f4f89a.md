#No vulnerability found for this question.

**Rationale:** The premise depends on a nested `execute_intents` call executing and mutating state *before* the outer `finalize()` runs, but this is impossible given NEAR's execution model as implemented here.

`Engine::execute_signed_intents` synchronously iterates all `signed` payloads via `execute_signed_intent`, mutating only the in-memory `Deltas<S>` wrapper, and only calls `self.finalize()` once every payload has been processed [1](#0-0) . `AuthCall::execute_intent` only calls `engine.state.auth_call(...)`, which schedules a `Promise` (`Self::do_auth_call(...)` or the wNEAR withdraw chain) and immediately `.detach()`es it [2](#0-1) . `.detach()` in `near-sdk` simply tells the SDK not to return the promise as the method's result — it does not synchronously execute anything. Per the NEAR protocol, a scheduled `Promise`'s callee (`on_auth`) always executes in a **separate, later receipt**, which can only begin execution after the current receipt (including the `#[private] do_auth_call`, and critically the outer `execute_intents` receipt with its `finalize()` call) has fully finished and its state changes have been committed to storage.

Therefore there is no code path by which a nested `on_auth`-triggered `execute_intents` call can run, or mutate contract storage, before the outer `execute_intents`'s `finalize()` invariant check (`TransferMatcher::finalize` in `contracts/defuse/core/src/engine/state/deltas.rs`) reads and validates the accumulated `Deltas`/`TokenDeltas` [3](#0-2) . Each `execute_intents` call is atomic within its own receipt: any subsequent nested call (triggered later via a promise) operates on the already-committed post-`finalize` state, not on a stale or partially-applied one, so there is no double-counting or invisible mid-batch mutation. The question's own proof-idea section acknowledges this ("`Promise` execution is always deferred to a new receipt in NEAR"), which confirms the binding holds by construction rather than being violated.

Since no reachable code path lets an attacker cause `finalize()`'s balance/delta reconciliation to diverge from the real committed state within a single receipt, and the described exploit requires synchronous re-entrancy that NEAR's async receipt model does not permit, this does not constitute a valid, reproducible vulnerability.

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

**File:** contracts/defuse/src/contract/intents/state.rs (L303-337)
```rust
    fn auth_call(&mut self, signer_id: &AccountIdRef, auth_call: AuthCall) -> Result<()> {
        if auth_call.attached_deposit.is_zero() {
            Self::do_auth_call(signer_id.to_owned(), auth_call)
        } else {
            // withdraw from signer's wNEAR balance
            self.withdraw(
                signer_id,
                [(
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    auth_call.attached_deposit.as_yoctonear(),
                )],
                Some("withdraw"),
                false,
            )?;

            ext_wnear::ext(self.wnear_id.clone())
                .with_attached_deposit(NearToken::from_yoctonear(1))
                .with_static_gas(NEAR_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .near_withdraw(U128(auth_call.attached_deposit.as_yoctonear()))
                .then(
                    // do_auth_call only after unwrapping NEAR
                    Self::ext(env::current_account_id())
                        .with_static_gas(
                            Self::auth_call_callback_gas(&auth_call)
                                .ok_or(DefuseError::GasOverflow)?,
                        )
                        .do_auth_call(signer_id.to_owned(), auth_call),
                )
        }
        .detach();

        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-283)
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
```
