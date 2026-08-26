# Q2158: attached deposit and predecessor spoofing in the context — vmstate.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a receipt shape where predecessor_account_id, signer_account_id, and attached_deposit can diverge, with the input length at exactly the host function's accepted maximum, reach `view_for_free` in `runtime/near-vm-runner/src/logic/vmstate.rs` and make a contract observe a predecessor or deposit that the runtime did not actually deliver, breaking the invariant that context values reported to WASM exactly match the receipt the runtime is executing, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-vm-runner/src/logic/vmstate.rs` :: `view_for_free`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a receipt shape where predecessor_account_id, signer_account_id, and attached_deposit can diverge; with the input length at exactly the host function's accepted maximum
- Exploit idea: make a contract observe a predecessor or deposit that the runtime did not actually deliver
- Invariant to test: context values reported to WASM exactly match the receipt the runtime is executing
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test comparing context values against the receipt
