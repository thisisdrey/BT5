# Q5625: bandwidth request granularity rounding — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipt sizes chosen to sit exactly on bandwidth-request granularity boundaries, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `get_receipt_group_sizes_for_buffer_to_shard` in `runtime/runtime/src/congestion_control.rs` and get granted bandwidth that is smaller than the receipt it must carry, stalling the link permanently, breaking the invariant that granted bandwidth always suffices for at least the head receipt of the buffer, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `get_receipt_group_sizes_for_buffer_to_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipt sizes chosen to sit exactly on bandwidth-request granularity boundaries; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: get granted bandwidth that is smaller than the receipt it must carry, stalling the link permanently
- Invariant to test: granted bandwidth always suffices for at least the head receipt of the buffer
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: unit test over receipt sizes at every granularity boundary
