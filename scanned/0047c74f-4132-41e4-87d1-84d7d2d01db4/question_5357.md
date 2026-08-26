# Q5357: protocol-version gating at the epoch boundary — signable_message.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a transaction only valid under the previous protocol version, timed at the first block of a new epoch, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `is_transaction` in `core/primitives/src/signable_message.rs` and have nodes disagree on whether the transaction is valid depending on the version they resolve, breaking the invariant that transaction validity is a deterministic function of the executing block's protocol version, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives/src/signable_message.rs` :: `is_transaction`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a transaction only valid under the previous protocol version, timed at the first block of a new epoch; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: have nodes disagree on whether the transaction is valid depending on the version they resolve
- Invariant to test: transaction validity is a deterministic function of the executing block's protocol version
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test-loop test upgrading protocol version mid-run and replaying the transaction
