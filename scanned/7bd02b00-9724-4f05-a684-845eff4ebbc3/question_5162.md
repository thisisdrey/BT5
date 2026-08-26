# Q5162: random_seed determinism and predictability — logic.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a contract that reads random_seed and branches on it, called at chosen heights, with the input length one byte past the accepted maximum, and additionally with a (ptr,len) pair whose sum overflows the address space, reach `gas_key_exec_pk_len` in `runtime/near-vm-runner/src/logic/logic.rs` and have the seed differ between chunk production and stateless re-execution, breaking the invariant that random_seed is fixed by the block and identical during validation, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `runtime/near-vm-runner/src/logic/logic.rs` :: `gas_key_exec_pk_len`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a contract that reads random_seed and branches on it, called at chosen heights; with the input length one byte past the accepted maximum; with a (ptr,len) pair whose sum overflows the address space
- Exploit idea: have the seed differ between chunk production and stateless re-execution
- Invariant to test: random_seed is fixed by the block and identical during validation
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: differential test comparing seed observed in production vs validation
