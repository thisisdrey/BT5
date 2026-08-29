Confirmed: this is a real (though narrowly scoped) finding.

### Title
`sync_data()` I/O failure in `FilesystemContractRuntimeCache::put` propagates as `VMRunnerError::CacheError`, causing `execute_function_call` to abort the entire chunk apply instead of failing only the triggering receipt - ([File: runtime/near-vm-runner/src/cache.rs])

### Summary
`FilesystemContractRuntimeCache::put` writes the compiled contract, calls `file.sync_data()?` (cache.rs:670), and only performs the rename/index-update afterward; a failed `sync_data()` returns an `io::Error` before that block runs. This bubbles up through `compile_and_persist`'s `cache.put(...).map_err(CacheError::WriteError)?` (wasmtime_runner/mod.rs:676) into the memory-cache lookup closure of `with_compiled_and_loaded`, ultimately surfacing to `near_vm_runner::run` as `Err(VMRunnerError::CacheError(...))`. In `execute_function_call`, this specific variant is *not* converted into a graceful `FunctionCallError`/no-op outcome like `ContractCodeNotPresent` or `LoadingError` are - it is instead mapped straight to `Err(StorageError::StorageInconsistentState(...).into())` (function_call.rs:325-330), which propagates as a hard `RuntimeError` that aborts the whole chunk apply rather than failing just the one receipt. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`put`'s only fallible step after the temp file is written is `file.sync_data()?` at cache.rs:670, occurring strictly before the rename/index-update critical section (cache.rs:672-698). Any `io::Error` here (e.g. `ENOSPC`/`EIO` under real disk pressure) returns `Err` from `put` directly. [4](#0-3) 

The call chain is: `WasmtimeVM::compile_and_persist` calls `cache.put(&key, record).map_err(CacheError::WriteError)?`, which is invoked from inside the closure passed to `cache.memory_cache().try_lookup(...)` in `with_compiled_and_loaded` (via `self.compile_and_cache(&code, cache)?` at mod.rs:726). The `?` there propagates through as `VMRunnerError` since the closure return type ultimately becomes `Result<_, VMRunnerError>` per `CacheError`'s conversion (`CacheError::WriteError` is one arm folded into `VMRunnerError::CacheError` upstream in `runner.rs`/`errors.rs`). [5](#0-4) 

In `execute_function_call`, the match over `near_vm_runner::run`'s result explicitly special-cases several `VMRunnerError` variants to produce graceful, receipt-scoped `FunctionCallError` outcomes (`ContractCodeNotPresent`, `LoadingError`), but `VMRunnerError::CacheError(err)` is treated as an unrecoverable inconsistency and turned into `Err(StorageError::StorageInconsistentState(...))`, which is a `RuntimeError` that propagates out of `action_function_call` and up through the whole chunk-apply pipeline, aborting apply for that chunk/shard rather than failing only the single receipt. [6](#0-5) 

Nothing in the existing signature/nonce/access-key/gas/storage-staking checks intercepts this, because the failure occurs deep in contract-loading logic after those checks have already passed and gas has been reserved for loading the executable.

### Impact Explanation
If an attacker can reliably induce local disk-write failures (e.g., via disk-pressure from a flood of poison contracts referenced elsewhere in this audit, item #10) at the moment a node processes a chunk containing a victim's yield-resume (or any) receipt, the resulting `sync_data()` failure aborts the entire chunk apply on that node rather than failing just the triggering receipt. This matches the "Liveness" invariant and "apply-path abort/halt affecting the shard" impact category: every receipt in that chunk, including any pending yielded promises, would be incidentally frozen/delayed on the affected node(s) until the underlying disk condition clears.

However, the practical severity is bounded: `StorageInconsistentState` errors in nearcore are treated as unrecoverable per-node database/storage inconsistencies and typically cause the affected node process to panic/crash rather than silently diverging consensus - other correctly-functioning validators/chunk-producers with healthy disks would not hit the fault, so this is a localized DoS/liveness issue on whichever node experiences the I/O fault, not a network-wide chain halt, unless the attacker can induce correlated disk pressure across a large fraction of chunk validators for the target shard simultaneously (a much higher bar, requiring exploiting item #10's poison-contract technique broadly and precisely-timed, which is speculative without independent proof that #10 can force out-of-disk conditions on demand across independent operators' infrastructure).

### Likelihood Explanation
Precondition: the attacker must have a demonstrated, reliable mechanism to induce local disk I/O failure (not just slow disk, but actual `sync_data` error) precisely coincident with processing a target chunk on a target node - this depends entirely on the unverified "item #10 poison-contract disk-pressure" technique referenced in the prompt, which is not established within this codebase/audit scope as a controllable, reliable I/O-failure primitive (ENOSPC/EIO under normal cloud storage is rare and self-healing, and most operators run cache directories on ample/expandable storage). Without a proven, reachable way for an unprivileged client to force `sync_data()` to fail deterministically and precisely-timed on a specific node, this is a low-likelihood, environment-dependent fault rather than a directly exploitable bug reachable purely through normal transaction/contract-deployment primitives.

### Recommendation
In `execute_function_call` (runtime/runtime/src/function_call.rs), treat `VMRunnerError::CacheError` from a *write* failure the same way as other recoverable cache/loading issues — return a receipt-scoped `FunctionCallError` (e.g., analogous to `LoadingError`) instead of unconditionally converting it to `StorageError::StorageInconsistentState`. Alternatively/additionally, in `FilesystemContractRuntimeCache::put` (runtime/near-vm-runner/src/cache.rs:670), treat `sync_data()` failure as a soft failure that still allows the calling contract execution to proceed using the freshly compiled in-memory module (skipping only the on-disk persistence), so a transient disk fault degrades caching performance rather than aborting the receipt/chunk.

### Proof of Concept
Unit test plan (in `runtime/near-vm-runner/src/tests/cache.rs` or an integration test in `runtime/runtime`):
1. Construct a `FilesystemContractRuntimeCache` backed by a directory whose `sync_data` calls can be forced to fail (e.g., wrap the file in a test double, or use a `tmpfs`/quota-limited directory to trigger `ENOSPC` on `fsync`, or inject via `cfg(test)` hook).
2. Call `compile_and_cache` / `with_compiled_and_loaded` for a fresh (uncached) contract so that `put` executes and its `sync_data()` fails.
3. Assert that `put` returns `Err`, and trace the propagated error through `compile_and_persist` → `with_compiled_and_loaded` → `near_vm_runner::run`.
4. Call `execute_function_call` with this contract and assert the current (buggy) behavior: it returns `Err(RuntimeError::StorageError(StorageError::StorageInconsistentState(_)))` rather than `Ok(VMOutcome { aborted: Some(FunctionCallError::_), .. })`.
5. After the fix, assert instead that `execute_function_call` returns `Ok(outcome)` with `outcome.aborted` set to a `FunctionCallError` (or that execution otherwise succeeds using the uncached in-memory module), and that only the single receipt is affected — verify sibling receipts/actions in the same chunk are processed normally in a runtime-level apply test.

### Citations

**File:** runtime/near-vm-runner/src/cache.rs (L630-678)
```rust
    fn put(&self, key: &CryptoHash, value: CompiledContractInfo) -> std::io::Result<()> {
        let weight = entry_disk_size(&value);

        const MAX_ATTEMPTS: u32 = 5;
        let final_filename = key.to_string();
        let mode = Mode::RUSR | Mode::WUSR | Mode::RGRP | Mode::WGRP;
        let flags = OFlags::CREATE | OFlags::TRUNC | OFlags::WRONLY;
        let mut attempt = 0;
        let (temp_filename, mut file) = loop {
            attempt += 1;
            let mut temporary_filename = final_filename.clone();
            temporary_filename.push('.');
            for b in rand::thread_rng().sample_iter(rand::distributions::Alphanumeric).take(8) {
                temporary_filename.push(b as char);
            }
            temporary_filename.push_str(".temp");
            match openat(&self.state.dir, &temporary_filename, flags, mode) {
                Ok(f) => break (temporary_filename, std::fs::File::from(f)),
                Err(e) if attempt > MAX_ATTEMPTS => return Err(e.into()),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e.into()),
            }
        };

        // This section manually "serializes" the data. The cache is quite sensitive to
        // unnecessary overheads and in order to enable things like mmap-based file access, we want
        // to have full control of what has been written.
        match value.compiled {
            CompiledContract::CompileModuleError(e) => {
                borsh::to_writer(&mut file, &e)?;
                file.write_all(&[ERROR_TAG])?;
            }
            CompiledContract::Code(bytes) => {
                file.write_all(&bytes)?;
                // Writing the tag at the end gives us well aligned buffer of the data above which
                // is necessary for 0-copy deserialization later on.
                file.write_all(&[CODE_TAG])?;
            }
        }
        file.write_all(&value.wasm_bytes.to_le_bytes())?;
        file.sync_data()?;
        drop(file);
        // Rename, index update, and victim unlinks share one lock, so a
        // concurrent `put` of a victim key can't rename a fresh file into place
        // before our unlink and have us delete it. The write above is outside
        // the lock.
        {
            let mut index = self.state.disk_index.lock();
            renameat(&self.state.dir, temp_filename, &self.state.dir, final_filename)?;
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L663-678)
```rust
    ) -> Result<CachedArtifact, CacheError> {
        // The cache may have been populated while we waited on the per-key lock.
        if let Some(compiled) = read_cache(cache, &key)? {
            return Ok(compiled);
        }
        let serialized_or_error = self.compile_uncached(code);
        let record = CompiledContractInfo {
            wasm_bytes: code.code().len() as u64,
            compiled: match &serialized_or_error {
                Ok(serialized) => CompiledContract::Code(serialized.clone()),
                Err(err) => CompiledContract::CompileModuleError(err.clone()),
            },
        };
        cache.put(&key, record).map_err(CacheError::WriteError)?;
        Ok(serialized_or_error)
    }
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L704-734)
```rust
        let (wasm_bytes, pre_result) = cache.memory_cache().try_lookup(
            key,
            || {
                is_memory_hit = false;
                let cache_record = cache.get(&key).map_err(CacheError::ReadError)?;
                let (wasm_bytes, module) =
                    if let Some(CompiledContractInfo { wasm_bytes, compiled }) = cache_record {
                        match compiled {
                            CompiledContract::CompileModuleError(err) => {
                                return Ok((
                                    err.size_bytes_approximate() as u64,
                                    to_any((wasm_bytes, Err(err))),
                                ));
                            }
                            CompiledContract::Code(module) => (wasm_bytes, module),
                        }
                    } else {
                        is_cache_hit = false;
                        let Some(code) = contract.get_code() else {
                            return Err(VMRunnerError::ContractCodeNotPresent);
                        };
                        let wasm_bytes = code.code().len() as u64;
                        match self.compile_and_cache(&code, cache)? {
                            Err(err) => {
                                return Ok((
                                    err.size_bytes_approximate() as u64,
                                    to_any((wasm_bytes, Err(err))),
                                ));
                            }
                            Ok(module) => (wasm_bytes, module),
                        }
```

**File:** runtime/runtime/src/function_call.rs (L290-341)
```rust
    let mut outcome = match result {
        Err(VMRunnerError::ContractCodeNotPresent) => {
            if runtime_ext.account().contract().is_some() {
                debug_assert!(
                    apply_state.apply_reason != ApplyChunkReason::UpdateTrackedShard,
                    "inconsistent state: contract code is missing from the trie, but the account has a non-empty contract"
                );

                // A missing body for an account that commits to a code hash is
                // witness incompleteness, not an execution result. Fail like any
                // other missing witness value rather than treating it as no-op.
                if apply_state.apply_reason == ApplyChunkReason::ValidateChunkStateWitness {
                    return Err(StorageError::MissingTrieValue(MissingTrieValue {
                        context: MissingTrieValueContext::TrieMemoryPartialStorage,
                        hash: contract_code_hash,
                    })
                    .into());
                }
            }
            let error = FunctionCallError::CompilationError(CompilationError::CodeDoesNotExist {
                account_id: account_id.as_str().into(),
            });
            return Ok(VMOutcome::nop_outcome(error));
        }
        Err(VMRunnerError::ExternalError(any_err)) => {
            let err: ExternalError =
                any_err.downcast().expect("Downcasting AnyError should not fail");
            return Err(match err {
                ExternalError::StorageError(err) => err.into(),
                ExternalError::ValidatorError(err) => RuntimeError::ValidatorError(err),
            });
        }
        Err(VMRunnerError::InconsistentStateError(
            err @ InconsistentStateError::IntegerOverflow,
        )) => return Err(StorageError::StorageInconsistentState(err.to_string()).into()),
        Err(VMRunnerError::CacheError(err)) => {
            metrics::FUNCTION_CALL_PROCESSED_CACHE_ERRORS
                .with_label_values::<&str>(&[(&err).into()])
                .inc();
            return Err(StorageError::StorageInconsistentState(err.to_string()).into());
        }
        Err(VMRunnerError::LoadingError(msg)) => {
            return Ok(VMOutcome::nop_outcome(FunctionCallError::LoadingError { msg }));
        }
        Err(VMRunnerError::Nondeterministic(msg)) => {
            panic!("Contract runner returned non-deterministic error '{}', aborting", msg)
        }
        Err(VMRunnerError::WasmUnknownError { debug_message }) => {
            panic!("Wasmer returned unknown message: {}", debug_message)
        }
        Ok(r) => r,
    };
```
