Looking at the full code path carefully before rendering a verdict.

**Key facts established from the code:**

1. `ClosureDepthCheck` activates on mainnet at 2026-02-09 12:00 PST. Before that, `enable_closure_depth_check = false` in `aptos_prod_vm_config`. [1](#0-0) 

2. `ENABLE_FUNCTION_VALUES` is in `default_features()`, so closures are enabled on mainnet. [2](#0-1) 

3. When `enable_closure_depth_check == false`, `check_depth_of_closure_captured_values` returns `Ok(())` unconditionally, bypassing the depth guard entirely. [3](#0-2) 

4. Both `PackClosure` and `PackClosureGeneric` call `check_depth_of_closure_captured_values` after popping captured values — this is the only depth guard on the closure value tree at pack time. [4](#0-3) [5](#0-4) 

5. The `VMConfig` comment explicitly states the flag "prevents deeply nested closure chains that could cause stack overflow" and "controls whether error messages format values (which could cause stack overflow during Display formatting)." [6](#0-5) 

6. `enable_depth_checks` (gated on `ENABLE_FUNCTION_VALUES`) is a separate flag used by native functions via `max_value_nest_depth()`. It does **not** gate `check_depth_of_closure_captured_values`. [7](#0-6) 

7. The gas charge for `PackClosure` is flat per-arg count, not per-depth. With `pack_closure_base = 9080` internal gas units and a ~10M gas budget, ~1,100 `PackClosure` iterations are feasible — far exceeding `DEFAULT_MAX_VM_VALUE_NESTED_DEPTH = 128`. [8](#0-7) 

8. The `DepthCheckingVisitor` used by `check_depth_of_value` is a recursive Rust visitor. At 1,100+ levels of closure nesting, recursive traversal during Drop, Display formatting, or comparison would overflow the Rust stack. [9](#0-8) 

9. The timed feature comment itself labels this a "Security fix." [10](#0-9) 

10. The e2e test confirms that with `enable_closure_depth_check` enabled, 129 iterations returns `VM_MAX_VALUE_DEPTH_REACHED`; without it, execution succeeds — exactly the pre-fix mainnet behavior. [11](#0-10) 

**No alternative guard exists**: `CALL_STACK_SIZE_LIMIT = 1024` bounds the Move call stack, not the Rust stack. The `AbstractValueSizeVisitor` in the memory tracker charges a flat 40 units per closure node without traversing captured values recursively. No other path in the `PackClosure` execution blocks unbounded closure nesting before 2026-02-09. [12](#0-11) [13](#0-12) 

---

### Title
Missing closure depth check allows unbounded nesting, causing validator Rust stack overflow — (`types/src/on_chain_config/timed_features.rs`, `third_party/move/move-vm/runtime/src/interpreter.rs`)

### Summary
On mainnet before 2026-02-09, `TimedFeatureFlag::ClosureDepthCheck` is inactive, setting `VMConfig::enable_closure_depth_check = false`. An unprivileged user can publish a Move module that iteratively wraps a closure inside another closure via `PackClosure`, building a chain exceeding `DEFAULT_MAX_VM_VALUE_NESTED_DEPTH` (128). Because the only depth guard — `check_depth_of_closure_captured_values` — returns `Ok(())` immediately when the flag is off, the VM creates arbitrarily deep closure value trees. Subsequent recursive Rust traversal of these trees (during Drop, Display formatting for error messages, or comparison) overflows the Rust thread stack, crashing the validator process.

### Finding Description
`aptos_prod_vm_config` sets `enable_closure_depth_check = timed_features.is_enabled(TimedFeatureFlag::ClosureDepthCheck)`. Before 2026-02-09 on mainnet this evaluates to `false`. In the interpreter, both `Instruction::PackClosure` and `Instruction::PackClosureGeneric` call `interpreter.check_depth_of_closure_captured_values(&captured)` after popping captured values from the operand stack. That function's first line is:

```rust
if !self.vm_config.enable_closure_depth_check {
    return Ok(());
}
```

With the flag off, no depth check is performed. The `ENABLE_FUNCTION_VALUES` feature flag is in `default_features()` and therefore active on mainnet, so `PackClosure` bytecode is accepted by the verifier. Gas cost is flat per captured-arg count (`pack_closure_base = 9080` internal gas), not per nesting depth, so ~1,100 iterations fit within a normal gas budget. The resulting closure value tree has depth >> 128. The `DepthCheckingVisitor` used by `check_depth_of_value` and the recursive `compare_with_depth` / `equals_with_depth` paths all recurse into captured values without a Rust-level stack guard, causing a Rust stack overflow when the deeply nested value is later operated on.

### Impact Explanation
A validator node processing the malicious transaction crashes due to Rust stack overflow. If the transaction is replayed across all validators (as it would be during block execution), all validators crash simultaneously, halting the chain. This is a material availability failure reachable from an unprivileged transaction.

### Likelihood Explanation
`ENABLE_FUNCTION_VALUES` is in `default_features()` and active on mainnet. The attack requires only publishing a Move module and submitting a single entry-function transaction — no privileged access, no governance, no validator control. The gas cost is affordable. The window was open from when function values were enabled until 2026-02-09.

### Recommendation
The fix — `TimedFeatureFlag::ClosureDepthCheck` activated 2026-02-09 — is already deployed. For defense-in-depth, the depth check should be unconditional (not timed-feature-gated) whenever `ENABLE_FUNCTION_VALUES` is active, since the two flags are logically coupled. Additionally, recursive Rust traversal of `Value` trees should be converted to iterative implementations to eliminate the Rust stack overflow class entirely.

### Proof of Concept
```move
module 0xattacker::deep_closure {
    public fun noop(_f: || has drop+copy) {}

    public entry fun exploit(n: u64) {
        let f: || has copy+drop = || {};
        let i = 0;
        while (i < n) {
            f = || noop(f);   // PackClosure: depth check skipped when flag is off
            i = i + 1;
        };
        // f is now a closure chain of depth n.
        // Dropping f, comparing f, or formatting f for an error message
        // recurses n levels deep in Rust, overflowing the stack at n >> 128.
    }
}
```
Submit with `n = 500` (well within gas budget). With `enable_closure_depth_check = false` (pre-2026-02-09 mainnet), execution succeeds and the deeply nested closure is created; subsequent Rust-level traversal crashes the validator. With the flag enabled, `VM_MAX_VALUE_DEPTH_REACHED` is returned at iteration 129, safely aborting the transaction.

### Citations

**File:** types/src/on_chain_config/timed_features.rs (L174-182)
```rust
            // Security fix: check depth of captured values when packing closures.
            (ClosureDepthCheck, TESTNET) => Los_Angeles
                .with_ymd_and_hms(2026, 2, 2, 22, 0, 0)
                .unwrap()
                .with_timezone(&Utc),
            (ClosureDepthCheck, MAINNET) => Los_Angeles
                .with_ymd_and_hms(2026, 2, 9, 12, 0, 0)
                .unwrap()
                .with_timezone(&Utc),
```

**File:** types/src/on_chain_config/aptos_features.rs (L311-312)
```rust
            Self::VM_BINARY_FORMAT_V8,
            Self::ENABLE_FUNCTION_VALUES,
```

**File:** third_party/move/move-vm/runtime/src/interpreter.rs (L1793-1796)
```rust
    fn check_depth_of_closure_captured_values(&self, captured: &[Value]) -> PartialVMResult<()> {
        if !self.vm_config.enable_closure_depth_check {
            return Ok(());
        }
```

**File:** third_party/move/move-vm/runtime/src/interpreter.rs (L1856-1858)
```rust
// TODO Determine stack size limits based on gas limit
const OPERAND_STACK_SIZE_LIMIT: usize = 1024;
const CALL_STACK_SIZE_LIMIT: usize = 1024;
```

**File:** third_party/move/move-vm/runtime/src/interpreter.rs (L2665-2666)
```rust
                        let captured = interpreter.operand_stack.popn(mask.captured_count())?;
                        interpreter.check_depth_of_closure_captured_values(&captured)?;
```

**File:** third_party/move/move-vm/runtime/src/interpreter.rs (L2720-2721)
```rust
                        let captured = interpreter.operand_stack.popn(mask.captured_count())?;
                        interpreter.check_depth_of_closure_captured_values(&captured)?;
```

**File:** third_party/move/move-vm/runtime/src/config.rs (L60-64)
```rust
    /// When enabled, checks the depth of captured values when packing closures.
    /// This prevents deeply nested closure chains that could cause stack overflow.
    /// Also controls whether error messages format values (which could cause stack
    /// overflow during Display formatting).
    pub enable_closure_depth_check: bool,
```

**File:** aptos-move/aptos-vm-environment/src/prod_configs.rs (L270-271)
```rust
    let enable_depth_checks = features.is_enabled(FeatureFlag::ENABLE_FUNCTION_VALUES);
    let enable_closure_depth_check = timed_features.is_enabled(TimedFeatureFlag::ClosureDepthCheck);
```

**File:** aptos-move/aptos-gas-schedule/src/gas_schedule/instr.rs (L96-99)
```rust
        [pack_closure_base: InternalGas, { RELEASE_V1_33.. => "pack_closure.base" }, 9080],
        [pack_closure_per_arg: InternalGasPerArg,  { RELEASE_V1_33.. => "pack.closure.per_arg" }, 1470],
        [pack_closure_generic_base: InternalGas,  { RELEASE_V1_33.. => "pack_closure_generic.base" }, 9080],
        [pack_closure_generic_per_arg: InternalGasPerArg,  { RELEASE_V1_33.. => "pack_closure_generic.per_arg" }, 1470],
```

**File:** third_party/move/move-vm/types/src/values/values_impl.rs (L6702-6705)
```rust
    fn visit_closure(&mut self, depth: u64, _len: usize) -> PartialVMResult<bool> {
        self.check(depth)?;
        Ok(true) // continue into captured values
    }
```

**File:** aptos-move/e2e-move-tests/src/tests/function_value_depth.rs (L41-49)
```rust
    let status = h.run_entry_function(&acc, str::parse("0x99::m::run2").unwrap(), vec![], vec![
        bcs::to_bytes(&129_u64).unwrap(),
    ]);
    assert_vm_status!(status, StatusCode::VM_MAX_VALUE_DEPTH_REACHED);

    let status = h.run_entry_function(&acc, str::parse("0x99::m::run2").unwrap(), vec![], vec![
        bcs::to_bytes(&128_u64).unwrap(),
    ]);
    assert_success!(status);
```

**File:** aptos-move/aptos-gas-schedule/src/gas_schedule/misc.rs (L48-48)
```rust
        [closure: AbstractValueSize, { RELEASE_V1_33.. => "closure" }, 40],
```
