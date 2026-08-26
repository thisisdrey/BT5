# Q2494: action receipt output data-id fan-out — receipt.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a receipt declaring the maximum number of output data receivers, several of them duplicated, when combined with a DeployContract earlier in the same action list, reach `output_data_receivers` in `core/primitives/src/receipt.rs` and have one data dependency satisfied twice so a callback runs more than once, breaking the invariant that each data dependency is satisfied exactly once per receipt, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `output_data_receivers`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a receipt declaring the maximum number of output data receivers, several of them duplicated; when combined with a DeployContract earlier in the same action list
- Exploit idea: have one data dependency satisfied twice so a callback runs more than once
- Invariant to test: each data dependency is satisfied exactly once per receipt
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test with duplicated output data receivers
