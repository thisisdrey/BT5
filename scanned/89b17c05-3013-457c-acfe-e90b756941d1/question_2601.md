# Q2601: state_viewer call_function reachable from public RPC — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, view arguments and a block reference chosen to hit an execution path that panics or diverges from replay, when combined with a DeployContract earlier in the same action list, reach `decode_universal_account_id` in `core/primitives-core/src/universal_account_id.rs` and make view execution disagree with the same call executed in a chunk, breaking the invariant that a view call never mutates state and never diverges from chunk execution semantics, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives-core/src/universal_account_id.rs` :: `decode_universal_account_id`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: view arguments and a block reference chosen to hit an execution path that panics or diverges from replay; when combined with a DeployContract earlier in the same action list
- Exploit idea: make view execution disagree with the same call executed in a chunk
- Invariant to test: a view call never mutates state and never diverges from chunk execution semantics
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: differential test running one method as a view and as a FunctionCall
