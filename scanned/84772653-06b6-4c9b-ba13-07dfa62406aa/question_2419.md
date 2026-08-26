# Q2419: zero-action and self-referential transactions — config.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, an action list of length zero, and a transaction whose signer_id equals receiver_id with a DeleteAccount action, with the boundary value chosen exactly at the enforced limit, reach `delegate_inner_action` in `runtime/runtime/src/config.rs` and reach an execution path that assumes at least one action or a distinct receiver, breaking the invariant that every accepted transaction has a valid, non-empty, self-consistent action list, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/runtime/src/config.rs` :: `delegate_inner_action`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: an action list of length zero, and a transaction whose signer_id equals receiver_id with a DeleteAccount action; with the boundary value chosen exactly at the enforced limit
- Exploit idea: reach an execution path that assumes at least one action or a distinct receiver
- Invariant to test: every accepted transaction has a valid, non-empty, self-consistent action list
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: runtime test applying the degenerate action lists and asserting a clean validation error
