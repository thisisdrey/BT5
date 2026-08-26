# Q0669: hash pre-image binding for transaction and receipt ids — utils.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, two structurally different payloads engineered to hash to the same id, with trailing bytes appended after a valid encoding, reach `create_receipt_id_from_transaction` in `core/primitives/src/utils.rs` and make one id refer to two different objects, breaking outcome attribution, breaking the invariant that ids commit to the complete, canonically encoded object, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/utils.rs` :: `create_receipt_id_from_transaction`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: two structurally different payloads engineered to hash to the same id; with trailing bytes appended after a valid encoding
- Exploit idea: make one id refer to two different objects, breaking outcome attribution
- Invariant to test: ids commit to the complete, canonically encoded object
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test asserting every field participates in the id
