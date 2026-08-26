# Q5319: fee config lookup for newly added action variants — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, an action variant added by the newest protocol version, batched with legacy actions, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `append_action_transfer` in `runtime/runtime/src/receipt_manager.rs` and hit a fee lookup that falls back to a zero or default cost for the new variant, breaking the invariant that every action variant has an explicit, non-zero, version-gated fee, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_transfer`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: an action variant added by the newest protocol version, batched with legacy actions; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: hit a fee lookup that falls back to a zero or default cost for the new variant
- Invariant to test: every action variant has an explicit, non-zero, version-gated fee
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: exhaustive match test asserting a fee exists for every Action variant
