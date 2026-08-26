# Q2331: storage_usage underflow on delete-heavy batches — errors.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, a batch deleting more keys/state than the account is accounted for, mixed with an AddKey in the same list, with the boundary value chosen exactly at the enforced limit, reach `metrics_label` in `core/primitives/src/errors.rs` and make storage_usage wrap below zero so the account appears to have effectively unlimited free storage, breaking the invariant that storage_usage is a faithful, never-underflowing byte count of the account's state, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/errors.rs` :: `metrics_label`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: a batch deleting more keys/state than the account is accounted for, mixed with an AddKey in the same list; with the boundary value chosen exactly at the enforced limit
- Exploit idea: make storage_usage wrap below zero so the account appears to have effectively unlimited free storage
- Invariant to test: storage_usage is a faithful, never-underflowing byte count of the account's state
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test asserting checked_sub on storage_usage in every delete path
