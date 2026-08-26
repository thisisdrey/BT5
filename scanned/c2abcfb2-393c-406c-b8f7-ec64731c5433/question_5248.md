# Q5248: promise_yield timeout accounting — function_call.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, a yielded promise resumed at exactly the timeout block, and one resumed one block late, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `execute_function_call` in `runtime/runtime/src/function_call.rs` and get both the timeout callback and the resume payload executed, or neither, breaking the invariant that a yielded promise resolves exactly once, by resume or by timeout, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/function_call.rs` :: `execute_function_call`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: a yielded promise resumed at exactly the timeout block, and one resumed one block late; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: get both the timeout callback and the resume payload executed, or neither
- Invariant to test: a yielded promise resolves exactly once, by resume or by timeout
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test resuming a yield at the exact timeout height
