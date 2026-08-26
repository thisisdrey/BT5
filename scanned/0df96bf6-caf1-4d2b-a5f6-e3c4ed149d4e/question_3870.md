# Q3870: transaction size limit boundary — verifier.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a SignedTransaction whose borsh encoding is exactly at, one under, and one over max_transaction_size, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `validate_transaction_actions` in `runtime/runtime/src/verifier.rs` and get an oversized transaction admitted so per-chunk size accounting is exceeded, breaking the invariant that no accepted transaction exceeds max_transaction_size after re-serialisation, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/runtime/src/verifier.rs` :: `validate_transaction_actions`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a SignedTransaction whose borsh encoding is exactly at, one under, and one over max_transaction_size; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: get an oversized transaction admitted so per-chunk size accounting is exceeded
- Invariant to test: no accepted transaction exceeds max_transaction_size after re-serialisation
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: unit test round-tripping a boundary-sized transaction through validation
