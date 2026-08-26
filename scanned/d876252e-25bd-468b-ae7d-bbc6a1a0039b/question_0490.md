# Q0490: congestion control interaction with promise yield timeouts — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, yields whose timeouts land while the shard is at maximum congestion, when a referencing account is deleted while others still reference the code, reach `get_nonce` in `runtime/runtime/src/global_contracts.rs` and have timeouts dropped or duplicated because congestion changes the processing path, breaking the invariant that timeout processing is independent of congestion state, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `get_nonce`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: yields whose timeouts land while the shard is at maximum congestion; when a referencing account is deleted while others still reference the code
- Exploit idea: have timeouts dropped or duplicated because congestion changes the processing path
- Invariant to test: timeout processing is independent of congestion state
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test firing timeouts under maximum congestion
