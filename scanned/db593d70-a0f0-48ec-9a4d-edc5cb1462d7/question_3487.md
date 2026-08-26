# Q3487: storage_write/read cost vs recorded proof size — utils.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, keys chosen to maximise trie node touches per byte written, with the input length at exactly the host function's accepted maximum, and additionally with the input length one byte past the accepted maximum, reach `null_terminated_method_names_len` in `runtime/near-vm-runner/src/logic/utils.rs` and make the gas charged far cheaper than the state-witness bytes the write forces to be recorded, breaking the invariant that storage gas costs bound the witness bytes a receipt can force to be recorded, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `runtime/near-vm-runner/src/logic/utils.rs` :: `null_terminated_method_names_len`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: keys chosen to maximise trie node touches per byte written; with the input length at exactly the host function's accepted maximum; with the input length one byte past the accepted maximum
- Exploit idea: make the gas charged far cheaper than the state-witness bytes the write forces to be recorded
- Invariant to test: storage gas costs bound the witness bytes a receipt can force to be recorded
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: test measuring recorded witness bytes per unit of storage gas
