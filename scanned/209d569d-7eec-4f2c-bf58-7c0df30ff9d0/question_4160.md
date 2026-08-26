# Q4160: balance-conservation checker bypass on cross-shard receipts — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a mix of transfers, refunds, and deletes crafted so per-chunk totals net out while individual accounts do not, when combined with a DeployContract earlier in the same action list, and additionally when combined with a DeleteAccount later in the same action list, reach `action_deterministic_state_init` in `runtime/runtime/src/deterministic_account_id.rs` and make the runtime's own balance check pass while real supply changes, breaking the invariant that the runtime balance checker accounts for every incoming and outgoing quantity, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/deterministic_account_id.rs` :: `action_deterministic_state_init`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a mix of transfers, refunds, and deletes crafted so per-chunk totals net out while individual accounts do not; when combined with a DeployContract earlier in the same action list; when combined with a DeleteAccount later in the same action list
- Exploit idea: make the runtime's own balance check pass while real supply changes
- Invariant to test: the runtime balance checker accounts for every incoming and outgoing quantity
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting the checker catches an injected imbalance
