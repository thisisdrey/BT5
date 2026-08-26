# Q0866: trie recording of missing keys — merkle_proof.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, reads of keys that do not exist, chosen to land on the deepest reachable prefix, with keys producing maximal-length extension nodes, reach `compute_past_block_proof_in_merkle_tree_of_later_block` in `core/store/src/merkle_proof.rs` and omit the proof of absence so validators cannot reproduce the negative result, breaking the invariant that proofs of absence are recorded for every negative lookup, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `core/store/src/merkle_proof.rs` :: `compute_past_block_proof_in_merkle_tree_of_later_block`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: reads of keys that do not exist, chosen to land on the deepest reachable prefix; with keys producing maximal-length extension nodes
- Exploit idea: omit the proof of absence so validators cannot reproduce the negative result
- Invariant to test: proofs of absence are recorded for every negative lookup
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: test re-executing a chunk whose receipts only read missing keys
