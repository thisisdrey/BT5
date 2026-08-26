# Q3561: protocol upgrade interacting with in-flight receipts — upgrade_schedule.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts created under version N that execute in the first chunk of version N+1, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `protocol_version_to_vote_for_at_date` in `core/primitives/src/upgrade_schedule.rs` and have the receipt priced or validated under a version that did not create it, breaking the invariant that in-flight receipts execute under well-defined, agreed-upon versioned rules, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/upgrade_schedule.rs` :: `protocol_version_to_vote_for_at_date`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts created under version N that execute in the first chunk of version N+1; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: have the receipt priced or validated under a version that did not create it
- Invariant to test: in-flight receipts execute under well-defined, agreed-upon versioned rules
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: test-loop test upgrading with receipts in flight
