# Q1992: promise API index validation — logic.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, promise indices that are out of range, freshly reused, or referencing a promise already resolved, with the input length at exactly the host function's accepted maximum, reach `decode` in `runtime/near-vm-runner/src/logic/logic.rs` and act on a promise the receipt does not own, breaking the invariant that promise indices are validated against the receipt's own promise set, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/logic.rs` :: `decode`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: promise indices that are out of range, freshly reused, or referencing a promise already resolved; with the input length at exactly the host function's accepted maximum
- Exploit idea: act on a promise the receipt does not own
- Invariant to test: promise indices are validated against the receipt's own promise set
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test over invalid promise indices
