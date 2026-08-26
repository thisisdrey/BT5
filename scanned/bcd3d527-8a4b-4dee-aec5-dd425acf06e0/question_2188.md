# Q2188: random_seed determinism and predictability — bls12381.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a contract that reads random_seed and branches on it, called at chosen heights, with the input length at exactly the host function's accepted maximum, reach `check_input_size` in `runtime/near-vm-runner/src/logic/bls12381.rs` and have the seed differ between chunk production and stateless re-execution, breaking the invariant that random_seed is fixed by the block and identical during validation, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `runtime/near-vm-runner/src/logic/bls12381.rs` :: `check_input_size`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a contract that reads random_seed and branches on it, called at chosen heights; with the input length at exactly the host function's accepted maximum
- Exploit idea: have the seed differ between chunk production and stateless re-execution
- Invariant to test: random_seed is fixed by the block and identical during validation
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: differential test comparing seed observed in production vs validation
