# Q2451: zero-action and self-referential transactions — transaction.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, an action list of length zero, and a transaction whose signer_id equals receiver_id with a DeleteAccount action, with the boundary value chosen exactly at the enforced limit, reach `from_nonce_and_index` in `core/primitives/src/transaction.rs` and reach an execution path that assumes at least one action or a distinct receiver, breaking the invariant that every accepted transaction has a valid, non-empty, self-consistent action list, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `core/primitives/src/transaction.rs` :: `from_nonce_and_index`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: an action list of length zero, and a transaction whose signer_id equals receiver_id with a DeleteAccount action; with the boundary value chosen exactly at the enforced limit
- Exploit idea: reach an execution path that assumes at least one action or a distinct receiver
- Invariant to test: every accepted transaction has a valid, non-empty, self-consistent action list
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: runtime test applying the degenerate action lists and asserting a clean validation error
