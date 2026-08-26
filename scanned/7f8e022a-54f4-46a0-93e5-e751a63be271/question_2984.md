# Q2984: congestion control interaction with promise yield timeouts — receipt.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, yields whose timeouts land while the shard is at maximum congestion, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `predecessor_id` in `core/primitives/src/receipt.rs` and have timeouts dropped or duplicated because congestion changes the processing path, breaking the invariant that timeout processing is independent of congestion state, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `predecessor_id`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: yields whose timeouts land while the shard is at maximum congestion; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: have timeouts dropped or duplicated because congestion changes the processing path
- Invariant to test: timeout processing is independent of congestion state
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test firing timeouts under maximum congestion
