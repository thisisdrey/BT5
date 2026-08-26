# Q3609: promise-yield timeout queue interaction with congestion — receipt.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, many yields created under congestion whose timeouts all fall in the same block, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `is_instant_receipt` in `core/primitives/src/receipt.rs` and concentrate timeout callbacks so one chunk must process far more than its gas limit allows, breaking the invariant that timeout processing is bounded per chunk and spills over safely, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `is_instant_receipt`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: many yields created under congestion whose timeouts all fall in the same block; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: concentrate timeout callbacks so one chunk must process far more than its gas limit allows
- Invariant to test: timeout processing is bounded per chunk and spills over safely
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: runtime test aligning many yield timeouts on one height
