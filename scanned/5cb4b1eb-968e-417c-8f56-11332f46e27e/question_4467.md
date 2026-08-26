# Q4467: bandwidth request granularity rounding — congestion_info.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipt sizes chosen to sit exactly on bandwidth-request granularity boundaries, when the shard is driven exactly onto a congestion threshold, and additionally when the shard oscillates across the congestion threshold every block, reach `finalize_allowed_shard` in `core/primitives/src/congestion_info.rs` and get granted bandwidth that is smaller than the receipt it must carry, stalling the link permanently, breaking the invariant that granted bandwidth always suffices for at least the head receipt of the buffer, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/primitives/src/congestion_info.rs` :: `finalize_allowed_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipt sizes chosen to sit exactly on bandwidth-request granularity boundaries; when the shard is driven exactly onto a congestion threshold; when the shard oscillates across the congestion threshold every block
- Exploit idea: get granted bandwidth that is smaller than the receipt it must carry, stalling the link permanently
- Invariant to test: granted bandwidth always suffices for at least the head receipt of the buffer
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: unit test over receipt sizes at every granularity boundary
