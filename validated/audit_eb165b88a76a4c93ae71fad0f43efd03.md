This confirms the design: the on-disk cache key `ContractCacheKey::Version5` is content-addressed on `code_hash`, `vm_config_non_crypto_hash`, `vm_kind`, and `vm_hash` [1](#0-0) . This means warming a compile against `next_config` writes an entry under a *different* cache key than the current config's key, and at actual execution time `action_function_call` looks up/computes the cache key using whatever `Config` is active *at that point in time* (i.e., `apply_state.config`, which by the time the FunctionCall receipt at block N executes has already been updated to the new epoch's config) [2](#0-1) . The lookup in `with_compiled_and_loaded` recomputes `get_contract_cache_key(contract.hash(), &self.config, self.vm_hash())` fresh from the config passed to the VM instance at execution time [3](#0-2) , so whether the entry was populated ahead of time by the warming background task or compiled cold at that exact moment, the result stored/read is keyed identically and deterministically by config content, not by wall-clock timing of when it was compiled. [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8) [10](#0-9)

### Citations

**File:** runtime/near-vm-runner/src/cache.rs (L44-65)
```rust
    Version5 {
        code_hash: CryptoHash,
        vm_config_non_crypto_hash: u64,
        vm_kind: near_parameters::vm::VMKind,
        vm_hash: u64,
    },
}

#[cfg(feature = "wasmtime_vm")]
pub(crate) fn get_contract_cache_key(
    code_hash: CryptoHash,
    config: &Config,
    vm_hash: u64,
) -> CryptoHash {
    let key = ContractCacheKey::Version5 {
        code_hash,
        vm_config_non_crypto_hash: config.non_crypto_hash(),
        vm_kind: config.vm_kind,
        vm_hash,
    };
    CryptoHash::hash_borsh(key)
}
```

**File:** runtime/runtime/src/lib.rs (L632-645)
```rust
                let account_contract = account.contract().into_owned();
                let contract_id = RuntimeContractIdentifier::resolve(
                    account_id,
                    account_contract,
                    &state_update,
                    &epoch_info_provider.chain_id(),
                    AccessOptions::DEFAULT,
                )?;
                let contract = preparation_pipeline.get_contract(
                    receipt,
                    contract_id.clone(),
                    action_index,
                    None,
                );
```

**File:** runtime/runtime/src/lib.rs (L647-660)
```rust
                action_function_call(
                    state_update,
                    apply_state,
                    account,
                    receipt,
                    action_receipt,
                    promise_results,
                    &mut result,
                    account_id,
                    function_call,
                    action_hash,
                    &contract_id,
                    &apply_state.config,
                    is_last_action,
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L570-597)
```rust
    pub(crate) fn compile_uncached(&self, code: &ContractCode) -> CachedArtifact {
        let start = std::time::Instant::now();
        let prepared_code = prepare::prepare_contract(code.code(), &self.config, VMKind::Wasmtime)
            .map_err(CompilationError::PrepareError)?;
        let serialized = self.engine.precompile_module(&prepared_code).map_err(|err| {
            tracing::debug!(
                target: "vm",
                ?err,
                code_hash = %code.hash(),
                code_size = code.code().len(),
                "wasmtime contract compilation failed",
            );
            CompilationError::WasmtimeCompileError { msg: err.to_string() }
        })?;

        let elapsed = start.elapsed();
        tracing::debug!(
            target: "vm",
            original_size = %code.code().len(),
            prepared_size = %prepared_code.len(),
            compiled_size = %serialized.len(),
            elapsed_ms = %elapsed.as_millis(),
            "wasmtime compiled contract",
        );

        crate::metrics::compilation_duration(elapsed);
        Ok(serialized)
    }
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L609-624)
```rust
    fn compile_and_cache(
        &self,
        code: &ContractCode,
        cache: &dyn ContractRuntimeCache,
    ) -> Result<CachedArtifact, CacheError> {
        let key = get_contract_cache_key(*code.hash(), &self.config, self.vm_hash());

        // Double-checked locking — outer step. An unlocked cache check before
        // touching `compilation_locks` lets already-cached cases skip both
        // mutex acquires entirely.
        if let Some(compiled) = read_cache(cache, &key)? {
            return Ok(compiled);
        }
        let entry = compilation_locks().entry(key);
        self.compile_and_persist(key, code, cache, entry.lock())
    }
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L635-654)
```rust
    fn try_compile_and_cache(
        &self,
        code: &ContractCode,
        cache: &dyn ContractRuntimeCache,
    ) -> Result<Option<CachedArtifact>, CacheError> {
        let key = get_contract_cache_key(*code.hash(), &self.config, self.vm_hash());
        if cache.has(&key).map_err(CacheError::ReadError)? {
            return Ok(None);
        }
        let entry = compilation_locks().entry(key);
        let Some(guard) = entry.try_lock() else {
            tracing::trace!(
                target: "vm",
                %key,
                "deferring warming compile to in-flight compiler"
            );
            return Ok(None);
        };
        self.compile_and_persist(key, code, cache, guard).map(Some)
    }
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L700-703)
```rust
        let mut is_cache_hit = true;
        let mut is_memory_hit = true;
        let key = get_contract_cache_key(contract.hash(), &self.config, self.vm_hash());
        cache.touch(&key);
```

**File:** test-loop-tests/src/tests/cache_warming.rs (L95-121)
```rust
    // The contract must already be cached under the new protocol's wasm_config.
    // No contract activity has run under new_protocol yet, so this can only
    // have been populated by pre-upgrade warming.
    let code_hash = CryptoHash::hash_bytes(near_test_contracts::backwards_compatible_rs_contract());
    let client = &env.test_loop.data.get(&client_handle).client;
    let next_runtime_config = client.runtime_adapter.get_runtime_config(new_protocol);
    let cache = client.runtime_adapter.compiled_contract_cache();
    let warmed = near_vm_runner::contract_cached(
        Arc::clone(&next_runtime_config.wasm_config),
        cache,
        code_hash,
    )
    .expect("compiled-contract cache lookup failed");
    assert!(warmed, "contract not cached under new-protocol wasm_config; warming did not work");

    // Post-upgrade sanity: the call succeeds under the new protocol.
    let post_call_tx = env.rpc_node().tx_call(
        &user,
        &user,
        "log_something",
        vec![],
        Balance::ZERO,
        Gas::from_teragas(300),
    );
    let outcome = env.rpc_runner().execute_tx(post_call_tx, Duration::seconds(10)).unwrap();
    assert_eq!(outcome.receipts_outcome[0].outcome.logs, vec!["hello"]);
}
```
