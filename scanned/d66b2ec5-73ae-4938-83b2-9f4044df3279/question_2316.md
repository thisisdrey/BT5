# Q2316: account balance host views during a receipt — dependencies.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, a contract reading account_balance and attached_deposit before and after issuing promises, with the input length at exactly the host function's accepted maximum, reach `get_receipt_receiver` in `runtime/near-vm-runner/src/logic/dependencies.rs` and observe balance that double-counts an already-committed outgoing transfer, breaking the invariant that balance visible to WASM reflects committed state exactly once, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/dependencies.rs` :: `get_receipt_receiver`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: a contract reading account_balance and attached_deposit before and after issuing promises; with the input length at exactly the host function's accepted maximum
- Exploit idea: observe balance that double-counts an already-committed outgoing transfer
- Invariant to test: balance visible to WASM reflects committed state exactly once
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting balance views across promise creation
