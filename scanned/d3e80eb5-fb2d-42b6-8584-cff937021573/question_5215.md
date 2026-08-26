# Q5215: cross-contract reentrancy through promise callbacks — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a callback that re-enters the original contract while its first invocation's state write is still pending, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `check_and_update_nonce` in `runtime/runtime/src/global_contracts.rs` and observe or write account state in an order the runtime does not commit atomically, breaking the invariant that state effects of a receipt are committed atomically before any dependent receipt observes them, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `check_and_update_nonce`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a callback that re-enters the original contract while its first invocation's state write is still pending; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: observe or write account state in an order the runtime does not commit atomically
- Invariant to test: state effects of a receipt are committed atomically before any dependent receipt observes them
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: runtime test asserting state visibility ordering across a re-entrant callback
