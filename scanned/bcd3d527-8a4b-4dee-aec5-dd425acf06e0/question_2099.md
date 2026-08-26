# Q2099: witness size limit hit by a single receipt — flat_store.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys, a receipt whose reads are chosen to maximise recorded bytes per gas unit, with keys producing maximal-length extension nodes, reach `remove_range_by_shard_uid` in `core/store/src/adapter/flat_store.rs` and produce a chunk whose witness exceeds the distribution limit, breaking the invariant that witness size stays within the limit for any gas-limited chunk, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `core/store/src/adapter/flat_store.rs` :: `remove_range_by_shard_uid`
- Entrypoint: attacker WASM driving `storage_write` / `storage_remove` / `storage_read` over attacker-chosen keys
- Attacker controls: a receipt whose reads are chosen to maximise recorded bytes per gas unit; with keys producing maximal-length extension nodes
- Exploit idea: produce a chunk whose witness exceeds the distribution limit
- Invariant to test: witness size stays within the limit for any gas-limited chunk
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: runtime test measuring witness bytes for the worst-case receipt
