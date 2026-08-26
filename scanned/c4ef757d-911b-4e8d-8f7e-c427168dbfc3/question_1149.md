# Q1149: bls12-381 subgroup and infinity handling — bls12381.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, points in the correct field but the wrong subgroup, plus the point at infinity in every argument slot, with the input length at exactly the host function's accepted maximum, reach `read_fp2_point` in `runtime/near-vm-runner/src/logic/bls12381.rs` and have subgroup checks skipped so verification accepts forged inputs, or make cost mispriced, breaking the invariant that all curve inputs are subgroup-checked and priced by their real cost, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/logic/bls12381.rs` :: `read_fp2_point`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: points in the correct field but the wrong subgroup, plus the point at infinity in every argument slot; with the input length at exactly the host function's accepted maximum
- Exploit idea: have subgroup checks skipped so verification accepts forged inputs, or make cost mispriced
- Invariant to test: all curve inputs are subgroup-checked and priced by their real cost
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test over wrong-subgroup and infinity inputs
