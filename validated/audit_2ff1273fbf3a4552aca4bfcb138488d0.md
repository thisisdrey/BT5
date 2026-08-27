#No vulnerability found for this question.

**Analysis:**

The described scenario matches intended, documented protocol behavior rather than a bug:

1. **Gas burnt on abort is expected and consistent with all guest errors.** When a `FunctionCall` action aborts with `HostError::RecordedStorageExceeded` (raised by `RecordedStorageCounter::observe_size`), it is a `FunctionCallError` that becomes `VMOutcome.aborted` and is committed on-chain — this is identical treatment to every other guest-triggered abort (`GasLimitExceeded`, `WasmTrap`, `ValueLengthExceeded`, etc.). Charging gas for work actually performed up to the abort point, while reverting state changes, is universal blockchain semantics (like "gas paid on revert"), not a value-conservation break. [1](#0-0) [2](#0-1) 

2. **Rollback discards the entire receipt's state changes, including any storage_usage delta**, so there is no "phantom" storage_usage persisted anywhere — `state_update.rollback()` clears `prospective` changes entirely, and the action-result error path (`set_error`) additionally clears queued receipts/proposals/burnt-amount bookkeeping. [3](#0-2) [4](#0-3) [5](#0-4) 

3. **No cross-node divergence is possible from proof-size accounting.** The `recorded_storage_size_upper_bound` mechanism is explicitly consensus-critical: it's part of stateless validation, where chunk validators independently recompute the same recorded proof and must agree with the chunk producer's accounting, or the chunk is rejected. The "memtrie vs disk trie" framing in the question has no support in the codebase — the upper-bound estimation is a deterministic, protocol-defined quantity checked by all validators, not an implementation-dependent side channel. [6](#0-5) [7](#0-6) 

4. **The `LackBalanceForState` check happens strictly after all actions in the receipt succeed** (`result.result.is_ok()`), so a `RecordedStorageExceeded` abort during action execution short-circuits the loop before the storage-staking check is ever reached — there is no race or "timing" window between the two checks; they are sequential and deterministic within a single apply call. [8](#0-7) 

There is no reachable path here causing theft/freezing of funds, double-spend, authorization escalation, or state-root divergence. The behavior described is by design and is exercised by existing tests such as `test_per_receipt_storage_proof_size_limit`. [9](#0-8)

### Citations

**File:** protocol-model/spec/contract-vm.md (L99-101)
```markdown
- **Determinism.** Gas values and the outcome (including a guest error) must be identical across validators; `VMRunnerError` (not `FunctionCallError`) signals node-local corruption and should crash/challenge rather than diverge (`runner.rs:16`, `errors.rs:12`).
- **Graceful guest errors vs. runner errors.** A `FunctionCallError` (`errors.rs:42`) — `CompilationError`, `LinkError`, `MethodResolveError`, `WasmTrap`, `HostError`, `LoadingError`, `WasmUnknownError` — becomes `VMOutcome.aborted` and is committed on-chain. A `VMRunnerError` propagates as `Err` from `run`.
- **Gas limits.** Never burn more than `min(prepaid_gas, max_gas_burnt)`; enforced in `burn_gas`/`process_gas_limit` (`gas_counter.rs:152`/`:169`). Out-of-gas ⇒ `GasLimitExceeded`/`GasExceeded`.
```

**File:** runtime/near-vm-runner/src/logic/recorded_storage_counter.rs (L19-33)
```rust
    pub fn observe_size(&mut self, latest_storage_proof_size: usize) -> Result<(), VMLogicError> {
        self.last_observed_storage_size = latest_storage_proof_size;

        let current_size = self.get_storage_size()?;
        if current_size > self.size_limit {
            let limit_u64 = self.size_limit.try_into().map_err(|_| {
                VMLogicError::InconsistentStateError(InconsistentStateError::IntegerOverflow)
            })?;
            return Err(VMLogicError::HostError(HostError::RecordedStorageExceeded {
                limit: ByteSize::b(limit_u64),
            }));
        }

        Ok(())
    }
```

**File:** runtime/runtime/src/lib.rs (L947-960)
```rust
                if let Err(ref mut res) = result.result {
                    res.index = Some(action_index as u64);
                    break;
                }
            }
        }

        // Going to check balance covers account's storage.
        if result.result.is_ok() {
            if let Some(ref account) = account {
                match check_storage_stake(account, account.amount(), &apply_state.config) {
                    Ok(()) => {
                        set_account(state_update, account_id.clone(), account);
                    }
```

**File:** runtime/runtime/src/lib.rs (L1024-1034)
```rust
        // Committing or rolling back state.
        match &result.result {
            Ok(_) => {
                state_update.commit(StateChangeCause::ReceiptProcessing {
                    receipt_hash: receipt.get_hash(),
                });
            }
            Err(_) => {
                state_update.rollback();
            }
        };
```

**File:** core/store/src/trie/update.rs (L225-228)
```rust
    pub fn rollback(&mut self) {
        self.prospective.clear();
        self.contract_storage.rollback_deploys();
    }
```

**File:** protocol-model/spec/runtime-execution.md (L148-149)
```markdown
- **Gas ordering**: `merge` asserts `gas_burnt_for_function_call <= gas_burnt <= gas_used` per action (`runtime/runtime/src/lib.rs:440`).
- **Failed receipt atomicity**: a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting (`runtime/runtime/src/lib.rs:967`). `set_error` additionally clears queued receipts, proposals, and burnt/subsidized amounts (`runtime/runtime/src/lib.rs:487`).
```

**File:** docs/misc/state_witness_size_limits.md (L42-45)
```markdown
### Validating the limits

Chunk validators have to verify that chunk producer respected all of the limits while producing the chunk. This means that validators also have to keep track of recorded storage proof by recording all trie accesses and they have to enforce the limits.
If it turns out that some limits weren't respected, the validators will generate a different result of chunk application and they won't endorse the chunk.
```

**File:** runtime/runtime/src/ext.rs (L313-324)
```rust
    fn get_recorded_storage_size(&self) -> usize {
        // `recorded_storage_size()` doesn't provide the exact size of storage proof
        // as it doesn't cover some corner cases (see https://github.com/near/nearcore/issues/10890),
        // so we use the `upper_bound` version to estimate how much storage proof
        // could've been generated by the receipt. As long as upper bound is
        // under the limit we can be sure that the actual value is also under the limit.
        self.trie_update.trie().recorded_storage_size_upper_bound()
    }

    fn storage_proof_size_before_receipt(&self) -> usize {
        self.storage_proof_size_before_receipt.unwrap_or_else(|| self.get_recorded_storage_size())
    }
```

**File:** runtime/runtime/src/tests/apply.rs (L1558-1567)
```rust
/// Test ProtocolFeature::EnforcePerReceiptStorageProofLimit. A receipt should record at most 4MB of
/// storage proof, no matter how many actions it has.
#[test]
fn test_per_receipt_storage_proof_size_limit() {
    // Number of distinct 1MB values written and then read, one per action.
    const NUM_VALUES: u8 = 5;

    const ACTION_GAS: Gas = Gas::from_teragas(800 / NUM_VALUES as u64);

    assert!(ProtocolFeature::EnforcePerReceiptStorageProofLimit.enabled(PROTOCOL_VERSION));
```
