# Q0849: host function determinism across VM kinds — utils.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, inputs that expose float rounding, NaN payloads, or platform-dependent behaviour, with the input length at exactly the host function's accepted maximum, reach `null_terminated_method_names_len` in `runtime/near-vm-runner/src/logic/utils.rs` and make the same contract produce different results under the configured VM than under replay, breaking the invariant that host functions are bit-for-bit deterministic across supported VM kinds and platforms, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/near-vm-runner/src/logic/utils.rs` :: `null_terminated_method_names_len`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: inputs that expose float rounding, NaN payloads, or platform-dependent behaviour; with the input length at exactly the host function's accepted maximum
- Exploit idea: make the same contract produce different results under the configured VM than under replay
- Invariant to test: host functions are bit-for-bit deterministic across supported VM kinds and platforms
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test executing the same WASM under each supported runner
