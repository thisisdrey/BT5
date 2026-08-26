# Q5102: transaction size limit boundary — mod.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a SignedTransaction whose borsh encoding is exactly at, one under, and one over max_transaction_size, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `is_delegate` in `core/primitives/src/action/mod.rs` and get an oversized transaction admitted so per-chunk size accounting is exceeded, breaking the invariant that no accepted transaction exceeds max_transaction_size after re-serialisation, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `core/primitives/src/action/mod.rs` :: `is_delegate`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a SignedTransaction whose borsh encoding is exactly at, one under, and one over max_transaction_size; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: get an oversized transaction admitted so per-chunk size accounting is exceeded
- Invariant to test: no accepted transaction exceeds max_transaction_size after re-serialisation
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: unit test round-tripping a boundary-sized transaction through validation
