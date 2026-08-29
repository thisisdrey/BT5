This confirms the described behavior exactly.

### Title
Contract loading fee charged after (not before) deserialize/link/instantiate work when `fix_contract_loading_cost` is disabled - (File: runtime/near-vm-runner/src/wasmtime_runner/mod.rs)

### Summary
When `config.fix_contract_loading_cost` is `false`, the legacy `after_loading_executable` path defers the `contract_loading_bytes * len` gas charge until after `with_compiled_and_loaded` has already performed cache lookup, disk read, `Module::deserialize`, linking, and `instantiate_pre` for the full contract. This lets an attacker trigger the full-size decode/link/instantiate cost with a `FunctionCallAction` whose attached gas is far below what the operation actually costs, since the abort/charge only happens after the expensive work is done.

### Finding Description
In `runtime/near-vm-runner/src/wasmtime_runner/mod.rs`, `with_compiled_and_loaded` performs cache lookup (`cache.memory_cache().try_lookup`), disk read (`cache.get`), `unsafe { Module::deserialize(&self.engine, &module) }`, linking (`link(&mut linker, &self.config)`), and `linker.instantiate_pre(&module)` — all real CPU/I/O work proportional to contract size — *before* any gas is charged for it. Only afterward does the code call: [1](#0-0) 

`before_loading_executable` only charges when `config.fix_contract_loading_cost` is true; otherwise it's a no-op, and the real charge happens in `after_loading_executable`, gated by `!config.fix_contract_loading_cost`: [2](#0-1) 

`add_contract_loading_fee` (`pay_per`/`pay_base`) can fail with `GasExceeded` if the accumulated burnt gas exceeds `prepaid_gas`, but that check happens strictly after `Module::deserialize` + `instantiate_pre` have already consumed CPU cycles for the entire contract code, regardless of the outcome. An attacker can deploy a `DeployContractAction` at `max_contract_size`, then send a `FunctionCallAction` with `gas` sized to cover only `new_action_receipt` + `function_call_base` (and no more), and the runtime will still perform the full deserialize/link/instantiate before rejecting with `GasExceeded`.

### Impact Explanation
This is a metering-totality violation: gas is charged strictly after the metered work happens rather than before, meaning the fee-and-abort decision is made too late to prevent the expensive work. Repeated at scale (many low-gas `FunctionCallAction`s targeting a max-size cached contract packed into a chunk), this inflates block/chunk application wall-clock/CPU time disproportionately to gas burnt, violating the "1ms = 1Tgas" pricing assumption used for gas-limit-based DoS protection. This is a resource-exhaustion / mispriced-computation issue rather than a direct fund-theft, double-spend, or state-root-divergence bug — it does not by itself cause loss of funds, consensus divergence, or chain halt; it only risks disproportionate chunk-application slowdown, bounded by the existing per-chunk gas limit and standard `max_contract_size` limits.

### Likelihood Explanation
- This code path is explicitly gated by `config.fix_contract_loading_cost`, which the audit note states is `false` on mainnet PV86 — i.e., this is legacy/compatibility behavior kept intentionally for old protocol versions, not a newly introduced flaw.
- Preconditions are cheap and fully within an unprivileged attacker's control: fund an account, deploy a max-size contract, submit low-gas `FunctionCallAction`s.
- Repeatability is bounded by the chunk's total gas limit (number of low-gas failing calls that fit per chunk) and by the fact that the wasted work is capped at one deserialize/instantiate per failing call — this is a known, historically-tracked tradeoff (see the `fix_contract_loading_cost` protocol feature itself, which exists specifically to charge the fee correctly going forward), not an unbounded/unpriced amplification vector introduced by new code.

### Recommendation
Enable `fix_contract_loading_cost` (already implemented as the corrected path) network-wide via a protocol upgrade so `before_loading_executable` pre-charges `contract_loading_bytes * wasm_code_bytes` prior to `Module::deserialize`/`instantiate_pre`, ensuring the fee-and-abort decision precedes the metered work rather than following it. Since this behavior is protocol-versioned and the fix already exists in code (`fix_contract_loading_cost = true` path), no additional code change is required beyond scheduling that protocol version to activate on mainnet.

### Proof of Concept
Integration/runtime-apply test:
1. Deploy a `DeployContractAction` with a wasm contract at `max_contract_size` bytes and let it be compiled/cached (first call with sufficient gas).
2. Send a second `FunctionCallAction` with `gas` set to just above `new_action_receipt` + `function_call_base` fees but well below `contract_loading_bytes * max_contract_size`.
3. Instrument `with_compiled_and_loaded` (or wrap the call) to measure wall-clock time / CPU cycles spent in `Module::deserialize` + `instantiate_pre` versus the gas actually burnt by the receipt (bounded to entry fees since the call aborts with `GasExceeded`).
4. Assert that measured time/CPU work is disproportionate to gas burnt (e.g., exceeds the 1ms≈1Tgas expectation from `runtime_params_estimator`), demonstrating that on `config.fix_contract_loading_cost == false`, the fee-and-abort happens after the expensive work, whereas with `fix_contract_loading_cost == true`, the call aborts before `Module::deserialize` is invoked at all (verify via `before_loading_executable` returning `Err` prior to reaching the memory-cache closure body).

### Citations

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L812-826)
```rust
        crate::metrics::record_compiled_contract_cache_lookup(is_cache_hit, is_memory_hit);
        let config = Arc::clone(&self.config);
        let result = gas_counter.before_loading_executable(&config, &method, wasm_bytes);
        if let Err(e) = result {
            let result = PreparationResult::OutcomeAbort(e);
            return Ok(PreparedContract { config, gas_counter, result });
        }
        match pre_result {
            Ok(res) => {
                let result = gas_counter.after_loading_executable(&config, wasm_bytes);
                if let Err(e) = result {
                    let result = PreparationResult::OutcomeAbort(e);
                    return Ok(PreparedContract { config, gas_counter, result });
                }
                closure(gas_counter, res)
```

**File:** runtime/near-vm-runner/src/logic/gas_counter.rs (L234-272)
```rust
    #[cfg(feature = "wasmtime_vm")]
    pub(crate) fn before_loading_executable(
        &mut self,
        config: &near_parameters::vm::Config,
        method_name: &str,
        wasm_code_bytes: u64,
    ) -> std::result::Result<(), super::errors::FunctionCallError> {
        if method_name.is_empty() {
            let error = super::errors::FunctionCallError::MethodResolveError(
                super::errors::MethodResolveError::MethodEmptyName,
            );
            return Err(error);
        }
        if config.fix_contract_loading_cost {
            if self.add_contract_loading_fee(wasm_code_bytes).is_err() {
                let error =
                    super::errors::FunctionCallError::HostError(super::HostError::GasExceeded);
                return Err(error);
            }
        }
        Ok(())
    }

    /// Legacy code to preserve old gas charging behaviour in old protocol versions.
    #[cfg(feature = "wasmtime_vm")]
    pub(crate) fn after_loading_executable(
        &mut self,
        config: &near_parameters::vm::Config,
        wasm_code_bytes: u64,
    ) -> std::result::Result<(), super::errors::FunctionCallError> {
        if !config.fix_contract_loading_cost {
            if self.add_contract_loading_fee(wasm_code_bytes).is_err() {
                return Err(super::errors::FunctionCallError::HostError(
                    super::HostError::GasExceeded,
                ));
            }
        }
        Ok(())
    }
```
