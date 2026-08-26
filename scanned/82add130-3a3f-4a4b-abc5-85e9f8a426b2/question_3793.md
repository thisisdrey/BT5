# Q3793: cross-shard receipt id derivation across layout versions — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts whose ids are derived at a layout boundary from attacker-chosen inputs, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `install_universal_account` in `runtime/runtime/src/universal_account_id.rs` and produce two receipts with the same id in different shards so one is dropped as duplicate, breaking the invariant that receipt ids are globally unique across shards and layout versions, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/universal_account_id.rs` :: `install_universal_account`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts whose ids are derived at a layout boundary from attacker-chosen inputs; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: produce two receipts with the same id in different shards so one is dropped as duplicate
- Invariant to test: receipt ids are globally unique across shards and layout versions
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: unit test on receipt-id derivation across layout versions
