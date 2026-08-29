### Title
Missing per-function body size check in `prepare_v2::CodeSectionEntry` allows bypass of `max_function_body_size` limit - (File: runtime/near-vm-runner/src/prepare/prepare_v2.rs)

### Summary
`prepare_v2::PrepareContext` has no `function_body_size_limit` field and its `CodeSectionEntry` arm only checks local counts, never comparing the function body byte size against `config.limit_config.max_function_body_size`, unlike `prepare_v3::PrepareContext` which explicitly tracks `function_body_size_limit` and returns `PrepareError::FunctionBodyTooLarge` when a function body exceeds it. This lets a contract routed through the v2 preparation path (non-reftypes, non-Wasmtime `vm_kind`) with a function body larger than `max_function_body_size` be accepted where v3 would reject it.

### Finding Description
`prepare_contract` in `runtime/near-vm-runner/src/prepare.rs` dispatches to `prepare_v3::prepare_contract` when `config.reftypes_bulk_memory || config.vm_kind == VMKind::Wasmtime`, and otherwise falls back to `prepare_v2::prepare_contract` [1](#0-0) . In `prepare_v3.rs`, `PrepareContext` maintains a `function_body_size_limit` field initialized from `limits.max_function_body_size.unwrap_or(u64::MAX)` [2](#0-1) , and its `CodeSectionEntry` handling checks the body size against that limit, returning `PrepareError::FunctionBodyTooLarge` on violation.

In contrast, `prepare_v2::PrepareContext` has no such field at all [3](#0-2) , and its `CodeSectionEntry` arm only iterates locals to enforce `local_limit`, then validates the function via `wasmparser`, without ever comparing `func`'s body byte length to `max_function_body_size` [4](#0-3) . The existing test `function_body_too_large` in `runtime/near-vm-runner/src/prepare.rs` uses `with_vm_variants` (iterating over all `VMKind`s, including those routed to v2) and asserts `Err(PrepareError::FunctionBodyTooLarge)` for a body over `max_function_body_size` [5](#0-4) , implying the invariant is intended to hold across all VM/prepare paths.

The only remaining backstop in v2 is the aggregate `max_instrumented_code_size` check applied after finite-wasm analysis and instrumentation completes [6](#0-5) , which limits total output size but does not prevent a single oversized function body from being processed by the finite-wasm gas/stack analysis and instrumentation passes, nor guarantee failure if the single function's contribution stays under the aggregate threshold.

### Impact Explanation
This corresponds to a size-limit bypass in the non-reftypes/non-Wasmtime prepare path (`prepare_v2.rs`). The scoped concern is that a per-function size guard depended upon by downstream metering/instrumentation assumptions (as encoded in the `function_body_too_large` test) is silently absent for this VM configuration, allowing an attacker's deployed contract to carry a function body larger than intended by `max_function_body_size` while remaining under `max_instrumented_code_size`, undetected by v2. Whether this manifests as an apply-path panic, or merely as inconsistent enforcement between VM kinds (a correctness/config-parity bug rather than a fund-loss or consensus-halting bug), was not fully determinable from the available code: I could not verify (due to tool exhaustion) whether `vm_kind`/`reftypes_bulk_memory` is attacker-controllable per-transaction or is fixed by the network's protocol config (i.e., whether an ordinary user can actually select the v2 path on mainnet), nor could I confirm the actual runtime consequence (panic vs. just a missing 400-style rejection) of processing an over-sized function body through the rest of the finite-wasm/instrumentation/execution pipeline.

### Likelihood Explanation
Reachability depends entirely on network configuration: `reftypes_bulk_memory` and `vm_kind` are protocol/runtime config values, not attacker-supplied transaction fields [7](#0-6) . If mainnet's current protocol config always sets `reftypes_bulk_memory = true` or `vm_kind = Wasmtime`, the v2 path is never reached by any contract regardless of attacker action, making this unreachable by an unprivileged attacker on mainnet. I was unable to confirm the current mainnet default before running out of iterations.

### Recommendation
Add a `function_body_size_limit` field to `prepare_v2::PrepareContext`, initialize it from `config.limit_config.max_function_body_size.unwrap_or(u64::MAX)` (mirroring `prepare_v3.rs`), and in the `CodeSectionEntry` arm compare the function body's byte length against this limit, returning `PrepareError::FunctionBodyTooLarge` on violation, to bring `prepare_v2.rs` in parity with `prepare_v3.rs` and satisfy the `function_body_too_large` test's cross-VM-kind invariant.

### Proof of Concept
The existing test `function_body_too_large` in `runtime/near-vm-runner/src/prepare.rs` (lines 162-179) already exercises this via `with_vm_variants`, asserting `PrepareError::FunctionBodyTooLarge` for a function body just over `max_function_body_size` created via `near_test_contracts::function_with_a_lot_of_nop(limit)`. Running this test against a VM-kind configuration that resolves to the v2 path (non-Wasmtime, `reftypes_bulk_memory = false`) would demonstrate whether v2 currently fails to reject such contracts (test failure/`Ok` result instead of `Err(FunctionBodyTooLarge)`). I was unable to execute this test to confirm actual pass/fail status due to tool-call exhaustion; a background agent with terminal access should run this test filtered to the v2-triggering `VMKind` to confirm.

### Citations

**File:** runtime/near-vm-runner/src/prepare.rs (L22-33)
```rust
pub fn prepare_contract(
    original_code: &[u8],
    config: &Config,
    kind: VMKind,
) -> Result<Vec<u8>, PrepareError> {
    let features = crate::features::WasmFeatures::new(config);
    if config.reftypes_bulk_memory || config.vm_kind == VMKind::Wasmtime {
        prepare_v3::prepare_contract(original_code, features, config, kind)
    } else {
        prepare_v2::prepare_contract(original_code, features, config, kind)
    }
}
```

**File:** runtime/near-vm-runner/src/prepare.rs (L162-179)
```rust
    #[test]
    fn function_body_too_large() {
        with_vm_variants(|kind| {
            let limit: u64 = 1000;
            let mut config = test_vm_config(Some(kind));
            config.limit_config.max_function_body_size = Some(limit);

            // A function body with nops just over the limit should be rejected.
            let wasm = near_test_contracts::function_with_a_lot_of_nop(limit);
            let r = prepare_contract(&wasm, &config, kind);
            assert_matches!(r, Err(PrepareError::FunctionBodyTooLarge));

            // A function body with nops just under the limit should be accepted.
            let wasm = near_test_contracts::function_with_a_lot_of_nop(limit / 2);
            let r = prepare_contract(&wasm, &config, kind);
            assert_matches!(r, Ok(_));
        });
    }
```

**File:** runtime/near-vm-runner/src/prepare/prepare_v3.rs (L14-44)
```rust
    function_body_size_limit: u64,
    table_limit: u32,
    table_element_limit: u64,
    type_limit: u64,
    global_limit: u64,
    validator: wp::Validator,
    func_validator_allocations: wp::FuncValidatorAllocations,
    before_import_section: bool,
    before_memory_section: bool,
    before_export_section: bool,
}

impl<'a> PrepareContext<'a> {
    fn new(code: &'a [u8], features: crate::features::WasmFeatures, config: &'a Config) -> Self {
        let limits = &config.limit_config;
        let table_element_limit = limits
            .max_elements_per_contract_table
            .map(u64::try_from)
            .transpose()
            .ok()
            .flatten()
            .unwrap_or(u64::MAX);
        Self {
            code,
            config,
            output_code: Vec::with_capacity(code.len()),
            // Practically reaching u64::MAX locals or functions is infeasible, so when the limit is not
            // specified, use that as a limit.
            function_limit: limits.max_functions_number_per_contract.unwrap_or(u64::MAX),
            local_limit: limits.max_locals_per_contract.unwrap_or(u64::MAX),
            function_body_size_limit: limits.max_function_body_size.unwrap_or(u64::MAX),
```

**File:** runtime/near-vm-runner/src/prepare/prepare_v2.rs (L7-21)
```rust
struct PrepareContext<'a> {
    code: &'a [u8],
    config: &'a Config,
    output_code: Vec<u8>,
    function_limit: u64,
    local_limit: u64,
    table_limit: u32,
    table_element_limit: u32,
    type_limit: u64,
    validator: wp::Validator,
    func_validator_allocations: wp::FuncValidatorAllocations,
    before_import_section: bool,
    before_memory_section: bool,
    before_export_section: bool,
}
```

**File:** runtime/near-vm-runner/src/prepare/prepare_v2.rs (L233-258)
```rust
                wp::Payload::CodeSectionEntry(func) => {
                    let local_reader =
                        func.get_locals_reader().map_err(|_| PrepareError::Deserialization)?;
                    for local in local_reader {
                        let (count, _ty) = local.map_err(|_| PrepareError::Deserialization)?;
                        self.local_limit = self
                            .local_limit
                            .checked_sub(u64::from(count))
                            .ok_or(PrepareError::TooManyLocals)?;
                    }

                    let func_validator = self
                        .validator
                        .code_section_entry(&func)
                        .map_err(|_| PrepareError::Deserialization)?;
                    // PANIC-SAFETY: no big deal if we panic here while the allocations are taken.
                    // Worst-case we are going to be making new allocations again, but in practice
                    // this should never happen as this context should not be reused.
                    let allocs = std::mem::replace(
                        &mut self.func_validator_allocations,
                        wp::FuncValidatorAllocations::default(),
                    );
                    let mut func_validator = func_validator.into_validator(allocs);
                    func_validator.validate(&func).map_err(|_| PrepareError::Deserialization)?;
                    self.func_validator_allocations = func_validator.into_allocations();
                }
```

**File:** runtime/near-vm-runner/src/prepare/prepare_v2.rs (L405-410)
```rust
    if let Some(max_size) = config.limit_config.max_instrumented_code_size {
        if res.len() as u64 > max_size {
            tracing::debug!(target: "vm", size=res.len(), ?kind, "instrumented code too large");
            return Err(PrepareError::InstrumentedCodeTooLarge);
        }
    }
```
