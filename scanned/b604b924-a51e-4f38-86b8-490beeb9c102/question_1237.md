# Q1237: flat storage delta application ordering — global_contract.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, writes to one key across several blocks in a fork-prone height range, with keys producing maximal-length extension nodes, reach `try_from` in `core/primitives-core/src/global_contract.rs` and apply deltas in an order that makes flat storage disagree with the trie, breaking the invariant that flat storage always reflects the trie for the block it claims to represent, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/global_contract.rs` :: `try_from`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: writes to one key across several blocks in a fork-prone height range; with keys producing maximal-length extension nodes
- Exploit idea: apply deltas in an order that makes flat storage disagree with the trie
- Invariant to test: flat storage always reflects the trie for the block it claims to represent
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test comparing flat-storage values against trie lookups after delta churn
