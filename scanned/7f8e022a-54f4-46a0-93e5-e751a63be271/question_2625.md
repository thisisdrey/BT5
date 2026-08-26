# Q2625: balance conservation across a failed transaction — signable_message.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, a batch where an early action succeeds and a later action fails after moving balance, with the boundary value chosen exactly at the enforced limit, reach `is_transaction` in `core/primitives/src/signable_message.rs` and leave the account with balance the rollback did not reclaim, or with balance credited twice, breaking the invariant that total supply plus all account balances is unchanged by any failed transaction, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/signable_message.rs` :: `is_transaction`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: a batch where an early action succeeds and a later action fails after moving balance; with the boundary value chosen exactly at the enforced limit
- Exploit idea: leave the account with balance the rollback did not reclaim, or with balance credited twice
- Invariant to test: total supply plus all account balances is unchanged by any failed transaction
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting the total-balance invariant after a mid-batch failure
