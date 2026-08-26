# Q0411: transaction validity window across a fork — flat_storage_resharder.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a transaction pinned to a block hash at the edge of the validity window, when transaction conversion cost alone approaches the chunk gas limit, reach `split_shard_task_postprocessing` in `chain/chain/src/resharding/flat_storage_resharder.rs` and get the same transaction accepted on one branch and expired on another, then replayed, breaking the invariant that a transaction executes at most once across all branches that survive, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `chain/chain/src/resharding/flat_storage_resharder.rs` :: `split_shard_task_postprocessing`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a transaction pinned to a block hash at the edge of the validity window; when transaction conversion cost alone approaches the chunk gas limit
- Exploit idea: get the same transaction accepted on one branch and expired on another, then replayed
- Invariant to test: a transaction executes at most once across all branches that survive
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: test-loop test replaying a window-edge transaction across a reorg
