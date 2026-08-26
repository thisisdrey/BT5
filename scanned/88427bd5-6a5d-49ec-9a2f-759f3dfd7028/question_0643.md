# Q0643: bandwidth request granularity rounding — receipts_column_helper.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipt sizes chosen to sit exactly on bandwidth-request granularity boundaries, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `next_back` in `core/store/src/trie/receipts_column_helper.rs` and get granted bandwidth that is smaller than the receipt it must carry, stalling the link permanently, breaking the invariant that granted bandwidth always suffices for at least the head receipt of the buffer, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/store/src/trie/receipts_column_helper.rs` :: `next_back`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipt sizes chosen to sit exactly on bandwidth-request granularity boundaries; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: get granted bandwidth that is smaller than the receipt it must carry, stalling the link permanently
- Invariant to test: granted bandwidth always suffices for at least the head receipt of the buffer
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: unit test over receipt sizes at every granularity boundary
