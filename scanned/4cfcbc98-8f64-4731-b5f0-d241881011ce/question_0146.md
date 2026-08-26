# Q0146: access-key nonce reuse across same-block transactions — transaction.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, two SignedTransactions sharing one (account_id, public_key) and nonce, submitted to different RPC nodes in the same block, with the boundary value chosen exactly at the enforced limit, reach `from_nonce` in `core/primitives/src/transaction.rs` and reach the nonce comparison and have both transactions pass verification and both be converted into action receipts, breaking the invariant that a given (account_id, public_key, nonce) is executed at most once for all time, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/transaction.rs` :: `from_nonce`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: two SignedTransactions sharing one (account_id, public_key) and nonce, submitted to different RPC nodes in the same block; with the boundary value chosen exactly at the enforced limit
- Exploit idea: reach the nonce comparison and have both transactions pass verification and both be converted into action receipts
- Invariant to test: a given (account_id, public_key, nonce) is executed at most once for all time
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime apply test asserting the second SignedTransaction returns InvalidNonce after the first is converted
