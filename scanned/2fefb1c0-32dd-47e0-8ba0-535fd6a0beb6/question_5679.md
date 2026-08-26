# Q5679: view client state query at a boundary block — view_client_actor.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, a query pinned to a block at an epoch or resharding boundary with a chosen shard id, when the same transaction is replayable across a reorg at the window edge, and additionally when execution depends on data the witness does not fully determine, reach `query_shard_uid` in `chain/client/src/view_client_actor.rs` and make the public RPC path resolve state from the wrong shard or layout, breaking the invariant that view queries resolve the shard layout of the queried block, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `chain/client/src/view_client_actor.rs` :: `query_shard_uid`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: a query pinned to a block at an epoch or resharding boundary with a chosen shard id; when the same transaction is replayable across a reorg at the window edge; when execution depends on data the witness does not fully determine
- Exploit idea: make the public RPC path resolve state from the wrong shard or layout
- Invariant to test: view queries resolve the shard layout of the queried block
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test querying across a layout boundary and comparing to chunk state
