# Q5057: cross-shard receipt id derivation across layout versions — upgrade_schedule.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts whose ids are derived at a layout boundary from attacker-chosen inputs, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `protocol_version_to_vote_for_at_date` in `core/primitives/src/upgrade_schedule.rs` and produce two receipts with the same id in different shards so one is dropped as duplicate, breaking the invariant that receipt ids are globally unique across shards and layout versions, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/upgrade_schedule.rs` :: `protocol_version_to_vote_for_at_date`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts whose ids are derived at a layout boundary from attacker-chosen inputs; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: produce two receipts with the same id in different shards so one is dropped as duplicate
- Invariant to test: receipt ids are globally unique across shards and layout versions
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test on receipt-id derivation across layout versions
