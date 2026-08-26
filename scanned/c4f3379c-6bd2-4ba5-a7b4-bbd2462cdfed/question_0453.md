# Q0453: transaction validity window across a fork — prepare_transactions.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a transaction pinned to a block hash at the edge of the validity window, when transaction conversion cost alone approaches the chunk gas limit, reach the primary handler in this file in `chain/client/src/prepare_transactions.rs` and get the same transaction accepted on one branch and expired on another, then replayed, breaking the invariant that a transaction executes at most once across all branches that survive, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `chain/client/src/prepare_transactions.rs` :: primary handler
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a transaction pinned to a block hash at the edge of the validity window; when transaction conversion cost alone approaches the chunk gas limit
- Exploit idea: get the same transaction accepted on one branch and expired on another, then replayed
- Invariant to test: a transaction executes at most once across all branches that survive
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: test-loop test replaying a window-edge transaction across a reorg
