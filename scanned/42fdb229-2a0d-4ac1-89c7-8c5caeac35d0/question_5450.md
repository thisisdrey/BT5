# Q5450: nonce upper-bound and block-height nonce rule — config.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a nonce set to u64::MAX, to exactly block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER, and to one past that bound, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `delegate_inner_action` in `runtime/runtime/src/config.rs` and make the upper-bound check saturate or wrap so a key's nonce space is exhausted or a stale nonce is accepted again, breaking the invariant that nonce is strictly increasing and always below the height-derived upper bound, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/config.rs` :: `delegate_inner_action`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a nonce set to u64::MAX, to exactly block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER, and to one past that bound; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: make the upper-bound check saturate or wrap so a key's nonce space is exhausted or a stale nonce is accepted again
- Invariant to test: nonce is strictly increasing and always below the height-derived upper bound
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test sweeping nonce boundary values against verify_and_charge_tx_ephemeral
