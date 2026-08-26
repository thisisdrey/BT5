# Q2605: protocol-version gating at the epoch boundary — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a transaction only valid under the previous protocol version, timed at the first block of a new epoch, with the boundary value chosen exactly at the enforced limit, reach `create_action_receipt` in `runtime/runtime/src/receipt_manager.rs` and have nodes disagree on whether the transaction is valid depending on the version they resolve, breaking the invariant that transaction validity is a deterministic function of the executing block's protocol version, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `create_action_receipt`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a transaction only valid under the previous protocol version, timed at the first block of a new epoch; with the boundary value chosen exactly at the enforced limit
- Exploit idea: have nodes disagree on whether the transaction is valid depending on the version they resolve
- Invariant to test: transaction validity is a deterministic function of the executing block's protocol version
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test-loop test upgrading protocol version mid-run and replaying the transaction
