# Q0067: gas charged after the work it is meant to pay for — utils.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, input sizes at the largest value the host function accepts, with the input length at exactly the host function's accepted maximum, reach `null_terminated_method_names_len` in `runtime/near-vm-runner/src/logic/utils.rs` and make the host function perform the expensive work before the gas charge, so an out-of-gas receipt still costs the node the work, breaking the invariant that every input-proportional operation charges gas before performing the work, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/utils.rs` :: `null_terminated_method_names_len`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: input sizes at the largest value the host function accepts; with the input length at exactly the host function's accepted maximum
- Exploit idea: make the host function perform the expensive work before the gas charge, so an out-of-gas receipt still costs the node the work
- Invariant to test: every input-proportional operation charges gas before performing the work
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: estimator/unit test measuring wall time for a receipt that runs out of gas mid-call
