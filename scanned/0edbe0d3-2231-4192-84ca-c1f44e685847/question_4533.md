# Q4533: trie recording of missing keys — manager.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, reads of keys that do not exist, chosen to land on the deepest reachable prefix, with a value stored directly at a full 16-way branch node, and additionally when the same value is written and deleted by many accounts in one chunk, reach `mark_ready_and_create_flat_storage` in `core/store/src/flat/manager.rs` and omit the proof of absence so validators cannot reproduce the negative result, breaking the invariant that proofs of absence are recorded for every negative lookup, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `core/store/src/flat/manager.rs` :: `mark_ready_and_create_flat_storage`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: reads of keys that do not exist, chosen to land on the deepest reachable prefix; with a value stored directly at a full 16-way branch node; when the same value is written and deleted by many accounts in one chunk
- Exploit idea: omit the proof of absence so validators cannot reproduce the negative result
- Invariant to test: proofs of absence are recorded for every negative lookup
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: test re-executing a chunk whose receipts only read missing keys
