# Q3403: flat storage value inlining boundary — construction.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, values whose size sits exactly at the inline/reference threshold, with keys producing maximal-length extension nodes, and additionally with a value stored directly at a full 16-way branch node, reach `new_branch` in `core/store/src/trie/mem/construction.rs` and make one path read the inlined value and another the referenced one, diverging, breaking the invariant that inlined and referenced values are always identical in content, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/mem/construction.rs` :: `new_branch`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: values whose size sits exactly at the inline/reference threshold; with keys producing maximal-length extension nodes; with a value stored directly at a full 16-way branch node
- Exploit idea: make one path read the inlined value and another the referenced one, diverging
- Invariant to test: inlined and referenced values are always identical in content
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test at the inlining threshold comparing both read paths
