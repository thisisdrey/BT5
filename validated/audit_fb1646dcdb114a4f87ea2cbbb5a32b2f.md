## Answer

#No Vulnerability found for this question.

**Analysis:**

The described race does not exist, and even in a hypothetical cache-miss scenario, the claimed consensus-divergence impact could not occur.

**1. No lock-window race in `put()`.** The rename, `disk_index.insert`, and victim `unlinkat` calls are all performed inside a single critical section guarded by `self.state.disk_index.lock()`: [1](#0-0) . Because the entire rename→insert→evict sequence for one `put()` call is atomic under this mutex, a second concurrent `put()` for a different key cannot interleave between another thread's rename and its own insert/eviction decision — the whole sequence for contract A completes (including any eviction) before contract B's `put()` can even begin acquiring the same lock. The comment directly above the lock explains this design intent: *"Rename, index update, and victim unlinks share one lock, so a concurrent put of a victim key can't rename a fresh file into place before our unlink and have us delete it."* [2](#0-1) . The `victim == key && key_stored` guard only handles the narrow case of a *same-key* replacement being reported as its own eviction victim by the underlying `LruWeightedCache::insert` [3](#0-2) , not a cross-key race — because no cross-key race window exists given the single mutex covering the full sequence.

**2. Even a genuine cache miss does not cause divergent `VMOutcome`.** The on-disk/in-memory cache is purely a performance optimization; a miss simply triggers `compile_and_cache`, which recompiles from the original wasm bytecode [4](#0-3) . Recompiling the same `code_hash` under the same `Config`/`vm_kind`/`vm_hash` is required to be functionally deterministic (that's exactly what `ContractCacheKey::Version5` encodes) [5](#0-4) . Critically, the gas charged for "loading" a contract is computed purely from `wasm_bytes` (code length) via `add_contract_loading_fee`/`contract_loading_bytes * code_len + contract_loading_base` [6](#0-5) , not from whether the artifact was served from cache or freshly compiled. Execution-time gas metering (`finite_wasm` instrumentation, `ExtCosts`) is likewise baked into the prepared/instrumented wasm before compilation, independent of caching. So whether a node hits or misses the on-disk cache changes only local latency, never the `burnt_gas`/`VMOutcome` that gets committed to the state root — precisely the invariant documented for this subsystem: *"No visible state shall rely upon timing taken for the certain operation, compilation or execution alike"* and *"Gas values and the outcome... must be identical across validators"* [7](#0-6) .

Both points independently rule out the claimed state-root-divergence / gas-divergence scenario: there is no exploitable race in `put()`'s locking, and even a real cache miss is consensus-neutral by design.

### Citations

**File:** runtime/near-vm-runner/src/cache.rs (L39-65)
```rust
enum ContractCacheKey {
    _Version1,
    _Version2,
    _Version3,
    _Version4,
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

**File:** runtime/near-vm-runner/src/cache.rs (L672-698)
```rust
        // Rename, index update, and victim unlinks share one lock, so a
        // concurrent `put` of a victim key can't rename a fresh file into place
        // before our unlink and have us delete it. The write above is outside
        // the lock.
        {
            let mut index = self.state.disk_index.lock();
            renameat(&self.state.dir, temp_filename, &self.state.dir, final_filename)?;
            // We just wrote the file, so its atime is current as of now.
            let evicted = index.insert(*key, weight, Instant::now());
            // `insert` returns `key` for a same-key replacement (keep our file)
            // and for an oversized reject (remove it); `contains` tells them apart.
            let key_stored = index.contains(key);
            for (victim, _) in evicted {
                if &victim == key && key_stored {
                    continue;
                }
                match unlinkat(&self.state.dir, victim.to_string(), AtFlags::empty()) {
                    Ok(()) | Err(Errno::NOENT) => {}
                    Err(err) => tracing::debug!(
                        target: "vm",
                        victim = %victim,
                        err = &err as &dyn std::error::Error,
                        "failed to unlink evicted compiled-contract cache file; on-disk cache may exceed its size limit"
                    ),
                }
            }
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

**File:** runtime/near-vm-runner/src/logic/gas_counter.rs (L216-227)
```rust
    /// Add a cost for loading the contract code in the VM.
    ///
    /// This cost does not consider the structure of the contract code, only the
    /// size. This is currently the only loading fee. A fee that takes the code
    /// structure into consideration could be added. But since that would have
    /// to happen after loading, we cannot pre-charge it. This is the main
    /// motivation to (only) have this simple fee.
    #[cfg(feature = "wasmtime_vm")]
    pub(crate) fn add_contract_loading_fee(&mut self, code_len: u64) -> Result<()> {
        self.pay_per(ExtCosts::contract_loading_bytes, code_len)?;
        self.pay_base(ExtCosts::contract_loading_base)
    }
```

**File:** runtime/near-vm-runner/FAQ.md (L121-129)
```markdown
### What are the dangers of bugs in compilers/VMs?

Unlike traditional software development, bugs and UB in the contract runtime could be pretty
devastating for the network coherence, as they may trigger inconsistency between nodes, and
lead to undesired blockchain forks. Thus, whenever there’s a risk of behavioral discrepancy
between nodes executing contract code - it shall be mitigated. No visible state shall rely
upon timing taken for the certain operation, compilation or execution alike, and if an
execution correctness problem exists - it must be the same on all nodes.
Thus compiler crashes are always preferred to potential hiding of undefined behavior.
```
