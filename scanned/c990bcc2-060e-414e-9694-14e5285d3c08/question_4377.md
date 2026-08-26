# Q4377: transaction validity window across a fork — v2.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a transaction pinned to a block hash at the edge of the validity window, when the pool is filled exactly to its bound by many attacker keys, and additionally when the same transaction is replayable across a reorg at the window edge, reach `get_children_shards_ids` in `core/primitives/src/shard_layout/v2.rs` and get the same transaction accepted on one branch and expired on another, then replayed, breaking the invariant that a transaction executes at most once across all branches that survive, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/shard_layout/v2.rs` :: `get_children_shards_ids`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a transaction pinned to a block hash at the edge of the validity window; when the pool is filled exactly to its bound by many attacker keys; when the same transaction is replayable across a reorg at the window edge
- Exploit idea: get the same transaction accepted on one branch and expired on another, then replayed
- Invariant to test: a transaction executes at most once across all branches that survive
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: test-loop test replaying a window-edge transaction across a reorg
