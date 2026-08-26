# Q5047: gas price selection between block boundaries — signable_message.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, transactions submitted exactly at a gas-price adjustment boundary, plus a pinned block hash near expiry, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `is_transaction` in `core/primitives/src/signable_message.rs` and have the price used for prepayment differ from the price used at execution, letting the attacker prepay less, breaking the invariant that gas is prepaid and burned at consistent, protocol-determined prices for the executing block, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/signable_message.rs` :: `is_transaction`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: transactions submitted exactly at a gas-price adjustment boundary, plus a pinned block hash near expiry; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: have the price used for prepayment differ from the price used at execution, letting the attacker prepay less
- Invariant to test: gas is prepaid and burned at consistent, protocol-determined prices for the executing block
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test comparing prepaid vs burnt gas cost across a gas-price change
