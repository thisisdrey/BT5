# Q0818: view client state query at a boundary block — utils.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, a query pinned to a block at an epoch or resharding boundary with a chosen shard id, when transaction conversion cost alone approaches the chunk gas limit, reach `map_keys_to_shard_id` in `core/primitives/src/shard_layout/utils.rs` and make the public RPC path resolve state from the wrong shard or layout, breaking the invariant that view queries resolve the shard layout of the queried block, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives/src/shard_layout/utils.rs` :: `map_keys_to_shard_id`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: a query pinned to a block at an epoch or resharding boundary with a chosen shard id; when transaction conversion cost alone approaches the chunk gas limit
- Exploit idea: make the public RPC path resolve state from the wrong shard or layout
- Invariant to test: view queries resolve the shard layout of the queried block
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test querying across a layout boundary and comparing to chunk state
