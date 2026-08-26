# Q0684: state witness recording completeness for deletes — construction.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, a receipt that deletes keys whose removal forces sibling nodes to be squashed, with keys producing maximal-length extension nodes, reach `pop_segment` in `core/store/src/trie/mem/construction.rs` and leave a node out of the recorded witness so re-execution cannot reach the same root, breaking the invariant that every node read or needed for re-execution is recorded in the witness, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `core/store/src/trie/mem/construction.rs` :: `pop_segment`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: a receipt that deletes keys whose removal forces sibling nodes to be squashed; with keys producing maximal-length extension nodes
- Exploit idea: leave a node out of the recorded witness so re-execution cannot reach the same root
- Invariant to test: every node read or needed for re-execution is recorded in the witness
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: differential test re-executing a chunk from its recorded witness alone
