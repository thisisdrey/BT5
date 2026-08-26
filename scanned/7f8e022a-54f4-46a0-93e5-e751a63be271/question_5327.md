# Q5327: gas attached exceeding max_total_prepaid_gas across a promise chain — adapter.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a deep promise chain where each level attaches nearly all remaining gas, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `view_access_keys` in `runtime/runtime/src/adapter.rs` and exceed the per-receipt or per-chunk prepaid-gas ceiling through accumulation, breaking the invariant that the sum of prepaid gas along any generated chain respects max_total_prepaid_gas, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/runtime/src/adapter.rs` :: `view_access_keys`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a deep promise chain where each level attaches nearly all remaining gas; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: exceed the per-receipt or per-chunk prepaid-gas ceiling through accumulation
- Invariant to test: the sum of prepaid gas along any generated chain respects max_total_prepaid_gas
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: runtime test measuring cumulative prepaid gas down a deep chain
