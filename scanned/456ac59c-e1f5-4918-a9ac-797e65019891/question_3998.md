# Q3998: promise_yield timeout accounting — adapter.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, a yielded promise resumed at exactly the timeout block, and one resumed one block late, when combined with a DeployContract earlier in the same action list, and additionally when combined with a DeleteAccount later in the same action list, reach `view_access_keys` in `runtime/runtime/src/adapter.rs` and get both the timeout callback and the resume payload executed, or neither, breaking the invariant that a yielded promise resolves exactly once, by resume or by timeout, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/adapter.rs` :: `view_access_keys`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: a yielded promise resumed at exactly the timeout block, and one resumed one block late; when combined with a DeployContract earlier in the same action list; when combined with a DeleteAccount later in the same action list
- Exploit idea: get both the timeout callback and the resume payload executed, or neither
- Invariant to test: a yielded promise resolves exactly once, by resume or by timeout
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test resuming a yield at the exact timeout height
