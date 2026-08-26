# Q2401: action-count and receipt-size limits per receipt — gas.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, an action list sized exactly at the max-actions and max-total-length limits with maximal method names and args, with the boundary value chosen exactly at the enforced limit, reach `saturating_sub` in `core/primitives-core/src/gas.rs` and produce a receipt above the limit that still gets generated and routed, breaking the invariant that generated receipts always satisfy the same limits validation enforces on inbound transactions, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `core/primitives-core/src/gas.rs` :: `saturating_sub`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: an action list sized exactly at the max-actions and max-total-length limits with maximal method names and args; with the boundary value chosen exactly at the enforced limit
- Exploit idea: produce a receipt above the limit that still gets generated and routed
- Invariant to test: generated receipts always satisfy the same limits validation enforces on inbound transactions
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: unit test measuring generated receipt size against the configured limit
