# Q4718: bls12-381 subgroup and infinity handling — dependencies.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, points in the correct field but the wrong subgroup, plus the point at infinity in every argument slot, with the input length one byte past the accepted maximum, and additionally with a (ptr,len) pair whose sum overflows the address space, reach `post_quantum_keys_enabled` in `runtime/near-vm-runner/src/logic/dependencies.rs` and have subgroup checks skipped so verification accepts forged inputs, or make cost mispriced, breaking the invariant that all curve inputs are subgroup-checked and priced by their real cost, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/logic/dependencies.rs` :: `post_quantum_keys_enabled`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: points in the correct field but the wrong subgroup, plus the point at infinity in every argument slot; with the input length one byte past the accepted maximum; with a (ptr,len) pair whose sum overflows the address space
- Exploit idea: have subgroup checks skipped so verification accepts forged inputs, or make cost mispriced
- Invariant to test: all curve inputs are subgroup-checked and priced by their real cost
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test over wrong-subgroup and infinity inputs
