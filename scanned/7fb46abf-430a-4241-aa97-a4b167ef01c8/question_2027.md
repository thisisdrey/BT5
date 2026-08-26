# Q2027: squash after delete leaving a non-canonical trie — storage.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, a delete sequence that leaves a branch with exactly one child and a value, with keys producing maximal-length extension nodes, reach `create_block_not_supported_error` in `core/store/src/flat/storage.rs` and produce two structurally different tries with the same key set and different roots, breaking the invariant that the trie is canonical: one key set maps to exactly one root, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/flat/storage.rs` :: `create_block_not_supported_error`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: a delete sequence that leaves a branch with exactly one child and a value; with keys producing maximal-length extension nodes
- Exploit idea: produce two structurally different tries with the same key set and different roots
- Invariant to test: the trie is canonical: one key set maps to exactly one root
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: property test comparing roots for identical key sets built by different paths
