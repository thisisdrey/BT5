# Q0010: trie node encoding round-trip on attacker-shaped keys — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, keys that produce maximal-length extension nodes and full 16-way branches with a value at the branch, with keys producing maximal-length extension nodes, reach `update_on_receipt_popped` in `core/store/src/trie/outgoing_metadata.rs` and produce a node whose decode differs from its encode so the state root diverges between nodes, breaking the invariant that trie node encoding is canonical and round-trips exactly for every reachable shape, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `update_on_receipt_popped`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: keys that produce maximal-length extension nodes and full 16-way branches with a value at the branch; with keys producing maximal-length extension nodes
- Exploit idea: produce a node whose decode differs from its encode so the state root diverges between nodes
- Invariant to test: trie node encoding is canonical and round-trips exactly for every reachable shape
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: fuzz test round-tripping generated nodes from attacker-shaped key sets
