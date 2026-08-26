# Q0451: memtrie arena allocation reuse after delete — global_contract.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, a workload that repeatedly allocates and frees nodes of the same size class, with keys producing maximal-length extension nodes, reach `try_from` in `core/primitives-core/src/global_contract.rs` and get a freed arena slot reused while still referenced, corrupting live state, breaking the invariant that no arena allocation is reused while any live node references it, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/global_contract.rs` :: `try_from`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: a workload that repeatedly allocates and frees nodes of the same size class; with keys producing maximal-length extension nodes
- Exploit idea: get a freed arena slot reused while still referenced, corrupting live state
- Invariant to test: no arena allocation is reused while any live node references it
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: stress test alternating insert/delete over one size class with root assertions
