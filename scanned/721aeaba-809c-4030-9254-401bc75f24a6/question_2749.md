# Q2749: nonce upper-bound and block-height nonce rule — signable_message.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a nonce set to u64::MAX, to exactly block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER, and to one past that bound, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `is_transaction` in `core/primitives/src/signable_message.rs` and make the upper-bound check saturate or wrap so a key's nonce space is exhausted or a stale nonce is accepted again, breaking the invariant that nonce is strictly increasing and always below the height-derived upper bound, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/signable_message.rs` :: `is_transaction`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a nonce set to u64::MAX, to exactly block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER, and to one past that bound; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: make the upper-bound check saturate or wrap so a key's nonce space is exhausted or a stale nonce is accepted again
- Invariant to test: nonce is strictly increasing and always below the height-derived upper bound
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test sweeping nonce boundary values against verify_and_charge_tx_ephemeral
