# Q4097: fee config lookup for newly added action variants — action_validation.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, an action variant added by the newest protocol version, batched with legacy actions, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `validate_function_call_action` in `runtime/runtime/src/action_validation.rs` and hit a fee lookup that falls back to a zero or default cost for the new variant, breaking the invariant that every action variant has an explicit, non-zero, version-gated fee, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/action_validation.rs` :: `validate_function_call_action`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: an action variant added by the newest protocol version, batched with legacy actions; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: hit a fee lookup that falls back to a zero or default cost for the new variant
- Invariant to test: every action variant has an explicit, non-zero, version-gated fee
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: exhaustive match test asserting a fee exists for every Action variant
