# Q2012: squash after delete leaving a non-canonical trie — contract.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, a delete sequence that leaves a branch with exactly one child and a value, with keys producing maximal-length extension nodes, reach `get` in `core/store/src/contract.rs` and produce two structurally different tries with the same key set and different roots, breaking the invariant that the trie is canonical: one key set maps to exactly one root, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/contract.rs` :: `get`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: a delete sequence that leaves a branch with exactly one child and a value; with keys producing maximal-length extension nodes
- Exploit idea: produce two structurally different tries with the same key set and different roots
- Invariant to test: the trie is canonical: one key set maps to exactly one root
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: property test comparing roots for identical key sets built by different paths
