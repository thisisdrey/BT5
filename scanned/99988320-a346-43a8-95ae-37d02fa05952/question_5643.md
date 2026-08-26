# Q5643: hash pre-image binding for transaction and receipt ids — types.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, two structurally different payloads engineered to hash to the same id, with a duplicate or out-of-range enum discriminant, and additionally with nesting at exactly the maximum accepted depth, reach `from_le_bytes` in `core/primitives-core/src/types.rs` and make one id refer to two different objects, breaking outcome attribution, breaking the invariant that ids commit to the complete, canonically encoded object, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives-core/src/types.rs` :: `from_le_bytes`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: two structurally different payloads engineered to hash to the same id; with a duplicate or out-of-range enum discriminant; with nesting at exactly the maximum accepted depth
- Exploit idea: make one id refer to two different objects, breaking outcome attribution
- Invariant to test: ids commit to the complete, canonically encoded object
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test asserting every field participates in the id
