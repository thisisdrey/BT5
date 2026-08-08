### Title
Attacker-controlled program account can trigger a validator-wide panic in `TransactionBatchProcessor::replenish_program_cache` - ([File: svm/src/transaction_processor.rs])

### Summary
`replenish_program_cache` unconditionally `.expect()`s that `load_program_with_pubkey` succeeds for every key returned by `filter_executable_program_accounts`: `svm/src/transaction_processor.rs:928-937`. The `key` used for the second, authoritative load is chosen by `global_program_cache.extract()` based on data gathered earlier (`filter_executable_program_accounts`, `svm/src/program_loader.rs:235-276`) while only holding a **read** lock on the global program cache — the account itself is *not* locked or re-validated at load time. If the program account referenced by a transaction is deleted/closed/reallocated by another transaction executing earlier in the very same batch (SVM processes non-conflicting-but-still-sequential entries in order per SIMD-83, and cooperative loading spans potentially many concurrently-scheduled batches touching the same program id), `load_program_with_pubkey` can return `None`/`Ok(None)` for that key, and the `.expect("called load_program_with_pubkey() with nonexistent account")` panics the whole process. This is structurally the same bug class as the Carapace `lockCapital()` issue: an untrusted, attacker-influenced piece of on-chain state (an “ownership”/existence check on a token/account) is assumed stable between a check and a later use, and when an unprivileged actor breaks that assumption the code takes an unrecoverable path (there: a revert that blocks state transition and drains funds from another party; here: a `panic!` that crashes the validator process entirely, which is more severe).

### Finding Description
- `filter_executable_program_accounts` (`svm/src/program_loader.rs:235-276`) inspects the current program account via `callbacks.get_account_shared_data(account_key)` and builds a `ProgramToLoad` entry if the account exists and is owned by a known loader.
- This list feeds into `TransactionBatchProcessor::replenish_program_cache` (`svm/src/transaction_processor.rs:894-972`), which loops, takes a **read** lock on `self.global_program_cache`, calls `extract()` to pick the next `key` to actually load, drops the read lock, and then calls:
```rust
let (program, last_modification_slot) = load_program_with_pubkey(
    account_loader,
    program_runtime_environment_for_execution,
    &key,
    self.slot,
    execute_timings,
)
.expect("called load_program_with_pubkey() with nonexistent account");
``` [1](#0-0) 
- Nothing re-validates that the account backing `key` still exists/is still a program at the moment of this second load. Between the earlier `filter_executable_program_accounts` check and this load, another transaction in the same batch (executed sequentially per SIMD-83, see the comment at `svm/src/transaction_processor.rs:467-470`) — or a concurrently scheduled sibling batch racing on the shared `global_program_cache` — can close/reassign the program account (e.g., via the BPF Loader v3/v4 "close"/"undeploy" instruction, which is callable by the program's own upgrade authority, an unprivileged role from the validator's perspective).
- `svm/src/transaction_processor.rs:990-992` explicitly documents this exact possibility elsewhere in the file ("Program account was closed") for `prepare_one_program_for_upcoming_feature_set`, but that path returns gracefully, whereas `replenish_program_cache`'s hot path does not — it panics instead.
- The existing unit test `test_replenish_program_cache_with_nonexistent_accounts` (`svm/src/transaction_processor.rs:1855-1882`) confirms the exact panic message and that hitting this branch simply requires `load_program_with_pubkey` to be called against a pubkey with no backing account — precisely what happens if the account is removed underneath the cache extraction.

### Impact Explanation
Reaching `.expect()` on `None` in `replenish_program_cache` calls `panic!`, which aborts the executing thread. In `TransactionBatchProcessor`, this is invoked directly inside the leader's/validator's transaction execution hot path (`load_and_execute_sanitized_transactions`, called from `Consumer::execute_and_commit_transactions_locked` in banking stage, and from replay in `blockstore_processor.rs`). A panic here is not contained by any `catch_unwind`; it propagates and crashes the worker thread executing transactions, and in leader/replay contexts this halts block production/replay for that node — a concrete node-level denial-of-service triggerable purely by ordinary (non-privileged) transaction submission that closes/reuses a program account referenced elsewhere in the same processing window. This matches the "concrete node panic" acceptance bar.

### Likelihood Explanation
Reachability depends on winning a genuine TOCTOU race between `filter_executable_program_accounts`'s account snapshot and the later `load_program_with_pubkey` call, and on the ability to close/undeploy a program account cheaply (BPF Loader v3 "Close" and Loader v4 program-close/undeploy instructions permit this by an unprivileged program authority) at a point where the same key is still queued as "missing" for a batch already committed to processing it. I was not able to fully trace, within the available context, whether Loader V3/V4 close semantics can be sequenced tightly enough within a single scheduling window to guarantee the race (this needs empirical confirmation with a running validator/test harness), so likelihood should be treated as **plausible but unconfirmed** rather than proven — this is the main uncertainty in this analog.

### Recommendation
In `replenish_program_cache` (and `prepare_one_program_for_upcoming_feature_set`'s analogous call), replace the `.expect(...)` with graceful handling: if `load_program_with_pubkey` returns `None`, treat the program as closed/missing (skip it / mark a tombstone in `program_cache_for_tx_batch`, and fail only the specific transaction(s) that require it with a normal `TransactionError`, e.g. `ProgramAccountNotFound`), mirroring the safe pattern already used in `prepare_one_program_for_upcoming_feature_set` at `svm/src/transaction_processor.rs:990-992`.

### Proof of Concept
Not independently reproduced against a live cluster in this analysis; existing repository evidence: [2](#0-1) 
demonstrates that calling `replenish_program_cache` with a `ProgramToLoad` key whose backing account does not exist deterministically panics with `"called load_program_with_pubkey() with nonexistent account"`. Constructing an end-to-end exploit would require: (1) deploying/upgrading a program under Loader v3/v4, (2) submitting, within the same processing window, one transaction that closes/undeploys the program account and another transaction (or a concurrently-scheduled batch) that still references that program id as an instruction program, timed so the second transaction's `filter_executable_program_accounts` snapshot occurs before the close but its `replenish_program_cache` load occurs after — this precise timing was not verified live and remains the open item for a Devin session with terminal/cluster access to confirm.

### Citations

**File:** svm/src/transaction_processor.rs (L928-937)
```rust
            let program_to_store = program_to_load.map(|key| {
                // Load, verify and compile one program.
                let (program, last_modification_slot) = load_program_with_pubkey(
                    account_loader,
                    program_runtime_environment_for_execution,
                    &key,
                    self.slot,
                    execute_timings,
                )
                .expect("called load_program_with_pubkey() with nonexistent account");
```

**File:** svm/src/transaction_processor.rs (L1855-1882)
```rust
    #[test]
    #[should_panic = "called load_program_with_pubkey() with nonexistent account"]
    fn test_replenish_program_cache_with_nonexistent_accounts() {
        let mock_bank = MockBankCallback::default();
        let account_loader = (&mock_bank).into();
        let fork_graph = Arc::new(RwLock::new(TestForkGraph {}));
        let batch_processor =
            TransactionBatchProcessor::new(0, 0, Arc::downgrade(&fork_graph), None);
        let program_runtime_environment_for_execution =
            batch_processor.program_runtime_environment_for_epoch(0);
        let key = Pubkey::new_unique();

        let mut program_cache_for_tx_batch = ProgramCacheForTxBatch::new(batch_processor.slot);

        batch_processor.replenish_program_cache(
            &account_loader,
            vec![ProgramToLoad {
                program_id: &key,
                loader: ProgramCacheEntryOwner::LoaderV3,
                match_criteria: ProgramCacheMatchCriteria::NoCriteria,
                last_modification_slot: 0,
            }],
            &program_runtime_environment_for_execution,
            &mut program_cache_for_tx_batch,
            &mut ExecuteTimings::default(),
            true,
            true,
        );
```
