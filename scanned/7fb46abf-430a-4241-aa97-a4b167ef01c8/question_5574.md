# Q5574: congestion control interaction with promise yield timeouts — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, yields whose timeouts land while the shard is at maximum congestion, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `own_congestion_info` in `runtime/runtime/src/congestion_control.rs` and have timeouts dropped or duplicated because congestion changes the processing path, breaking the invariant that timeout processing is independent of congestion state, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `own_congestion_info`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: yields whose timeouts land while the shard is at maximum congestion; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: have timeouts dropped or duplicated because congestion changes the processing path
- Invariant to test: timeout processing is independent of congestion state
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test firing timeouts under maximum congestion
