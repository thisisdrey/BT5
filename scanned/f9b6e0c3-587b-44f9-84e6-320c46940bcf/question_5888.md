# Q5888: flat storage value inlining boundary — alloc.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, values whose size sits exactly at the inline/reference threshold, when the same value is written and deleted by many accounts in one chunk, and additionally when a delete forces sibling nodes to be squashed, reach `new_with_initial_stats` in `core/store/src/trie/mem/arena/alloc.rs` and make one path read the inlined value and another the referenced one, diverging, breaking the invariant that inlined and referenced values are always identical in content, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/mem/arena/alloc.rs` :: `new_with_initial_stats`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: values whose size sits exactly at the inline/reference threshold; when the same value is written and deleted by many accounts in one chunk; when a delete forces sibling nodes to be squashed
- Exploit idea: make one path read the inlined value and another the referenced one, diverging
- Invariant to test: inlined and referenced values are always identical in content
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test at the inlining threshold comparing both read paths
