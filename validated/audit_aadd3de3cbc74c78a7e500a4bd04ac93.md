### Title
Contract loading/preparation gas fee is not charged on compile or preparation failure prior to `fix_contract_loading_cost` activation - ([File: runtime/near-vm-runner/src/logic/gas_counter.rs])

### Summary
On the currently active protocol version modeled in this repo (PV 86, per `protocol-model/spec/contract-vm.md`), the `fix_contract_loading_cost` config flag is `false`, meaning the loading fee for a `FunctionCall` is charged **after** the contract has been loaded/compiled rather than pre-charged before deserialization/compilation is attempted. When compilation or preparation of the WASM module fails, the function returns an error before `after_loading_executable` (which applies the size-dependent loading fee) is ever reached, so **zero gas is burnt** for a failed load, exactly mirroring the "gas not consumed on precompile failure" bug class in the referenced Nibiru report.

### Finding Description
`WasmtimeVM::with_compiled_and_loaded` resolves a contract's compiled artifact through the on-disk/in-memory cache; on a miss it calls `compile_and_cache`, and a compile failure is captured as `CompiledContract::CompileModuleError` and returned as an `Err` almost immediately [1](#0-0) . The size-dependent loading fee is only applied via `GasCounter::after_loading_executable`, which is gated on `config.fix_contract_loading_cost`:

```
pub(crate) fn after_loading_executable(...) {
    if !config.fix_contract_loading_cost {
        if self.add_contract_loading_fee(wasm_code_bytes).is_err() { ... }
    }
    Ok(())
}
``` [2](#0-1) 

Because `fix_contract_loading_cost` is `false` on the active protocol version (PV 86; the fix only activates at PV 129 and is documented as nightly-only), the legacy post-load charging path is in effect [3](#0-2) . Under this ordering, any error surfaced during `prepare_contract`/deserialization/compilation/instantiation (i.e. malformed WASM, bad imports, bad exported memory, oversized module structure, etc.) causes the function to return before the fee-charging code is ever executed, resulting in zero gas burnt for the attempt. This is confirmed directly by the repository's own regression test:

```
test_builder()
    .wat(r#"(module (export "main" (func 0)))"#)
    .protocol_version(FIX_CONTRACT_LOADING_COST)
    .expects(&[
        expect![[r#"
            VMOutcome: balance 4 storage_usage 12 return data None burnt gas 0 used gas 0
            Err: PrepareError: Error happened while deserializing the module.
        "#]],
        expect![[r#"
            VMOutcome: balance 4 storage_usage 12 return data None burnt gas 55053273 used gas 55053273
            Err: PrepareError: Error happened while deserializing the module.
        "#]],
    ]);
``` [4](#0-3) 

The test explicitly labels the first (pre-fix) branch as `burnt gas 0` while the same input after the fix burns `55053273` gas — i.e., the exact "gas is not consumed when the operation fails" pattern described in the Nibiru report, but here located in the smart-contract loading pipeline instead of a Cosmos precompile.

Note that this stands in contrast to nearly all other host functions in `near-vm-runner` (e.g. `promise_batch_action_transfer`, `value_return`, `promise_batch_action_deploy_contract`), which consistently call `pay_base`/`pay_action_accumulated` *before* performing the fallible work, so an error there still burns the already-charged base gas [5](#0-4) . The contract-loading path is the one place where charging happens strictly *after* the fallible operation, on the legacy/current-mainnet code path.

### Impact Explanation
Since prepaid gas for a `FunctionCall` action is bought upfront by the transaction sender, and the sender fully controls the WASM bytes deployed to their own account, an attacker can deploy a contract crafted to always fail preparation/compilation (e.g. malformed sections, bad imports, oversized structure that triggers `PrepareError`) and then send `FunctionCall` receipts against it. Each such call forces the validator/chunk-producer to run the (potentially large) preparation/deserialization/compilation pipeline without any gas being burnt for the attempt, up to the point of failure. Because the failure path returns from `with_compiled_and_loaded` free of charge, an attacker can repeatedly trigger this expensive CPU work for the fixed "send" gas fee only, without paying anything for the actual node-side compute expended on the invalid module, i.e., resource consumption unaccounted for by gas — the same class of impact (uncompensated compute usage / potential chunk-processing DoS) as the referenced Nibiru finding.

Whether this rises to a shard-halting-severity DoS or a more moderate "free CPU cycles" issue depends on how many bytes of adversarial WASM can be pushed through preparation/compile before it is rejected (this is bounded by `max_contract_size`/`LimitConfig`, but compilation cost for a large near-maximum-size malformed module is still significant and paid for by the honest validator, not the attacker).

### Likelihood Explanation
This is fully reachable by an ordinary, unprivileged account: deploy any deliberately malformed contract via `DeployContract` (or `promise_batch_action_deploy_contract`) and then call it via a normal signed transaction/`FunctionCall`. No special node access, staking, or governance permission is required, and the affected code path (`fix_contract_loading_cost == false`) is the active behavior on the protocol version modeled by this repository (PV 86); the mitigating flag only activates at PV 129, which the repo documents as not yet active on mainnet in this snapshot.

### Recommendation
Charge (or reserve) the loading fee before attempting compilation/preparation regardless of `fix_contract_loading_cost`, i.e., always pre-charge based on `wasm_code_bytes` prior to invoking `compile_and_cache`/`prepare_contract`, mirroring the `before_loading_executable` pre-charge path that is already gated behind the fixed flag. This closes the free-attempt window for all protocol versions rather than only from PV 129 onward, consistent with the general pattern used by other host functions of paying `base`/`per_byte` fees before performing potentially-failing work.

### Proof of Concept
1. Deploy an account and use `DeployContract` to install a WASM module engineered to fail preparation deterministically (e.g. the malformed-export module used in the repo's own test, `(module (export "main" (func 0)))`, or a module with `bad_import_global`/`bad_import_func`).
2. Send a `FunctionCall` action against this contract's `main` method with the protocol version fixed such that `fix_contract_loading_cost == false` (the current, non-nightly mainnet setting per `protocol-model/spec/contract-vm.md`).
3. Observe the resulting `VMOutcome`: `burnt_gas == 0`/`used_gas == 0` despite the node having performed WASM deserialization/compilation work, exactly as shown in `test_fn_loading_gas_protocol_upgrade_fail_preparing` [6](#0-5) .
4. Repeat step 2 in a loop (bounded only by the sender's ability to pay the outer transaction `send` fee, which is far smaller than the compute cost of repeated failed compilations) to consume node compute resources without corresponding gas burn.

### Citations

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L726-734)
```rust
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

**File:** runtime/near-vm-runner/src/logic/gas_counter.rs (L257-272)
```rust
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

**File:** protocol-model/spec/contract-vm.md (L36-37)
```markdown
3. `before_loading_executable` (`gas_counter.rs:236`): reject empty `method_name` (`MethodResolveError::MethodEmptyName`); if `fix_contract_loading_cost` is set, pre-charge `add_contract_loading_fee` (`contract_loading_base` + `contract_loading_bytes * code_len`, `gas_counter.rs:225`) — on OOG return `HostError::GasExceeded` as an abort.
4. `after_loading_executable` (`gas_counter.rs:260`): if `fix_contract_loading_cost` is **not** set, charge the loading fee *after* loading instead (legacy ordering). On 2.13.0 mainnet `fix_contract_loading_cost` is `false` (the fix is nightly-only, PV 129), so the loading fee is charged post-load.
```

**File:** runtime/near-vm-runner/src/tests/runtime_errors.rs (L991-1013)
```rust
    #[test]
    fn test_fn_loading_gas_protocol_upgrade_fail_preparing() {
        // This list covers all control flows that are expected to change
        // with the protocol feature.
        // Having a test for every possible preparation error would be even
        // better, to ensure triggering any of them will always remain
        // compatible with versions before this upgrade. Unfortunately, we
        // currently do not have tests ready to trigger each error.

        #[allow(deprecated)]
        test_builder()
            .wat(r#"(module (export "main" (func 0)))"#)
            .protocol_version(FIX_CONTRACT_LOADING_COST)
            .expects(&[
                expect![[r#"
                    VMOutcome: balance 4 storage_usage 12 return data None burnt gas 0 used gas 0
                    Err: PrepareError: Error happened while deserializing the module.
                "#]],
                expect![[r#"
                    VMOutcome: balance 4 storage_usage 12 return data None burnt gas 55053273 used gas 55053273
                    Err: PrepareError: Error happened while deserializing the module.
                "#]],
            ]);
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3143-3150)
```rust
    ) -> Result<()> {
        self.result_state.gas_counter.pay_base(base)?;
        if self.context.is_view() {
            return Err(HostError::ProhibitedInView {
                method_name: "promise_batch_action_transfer".to_string(),
            }
            .into());
        }
```
