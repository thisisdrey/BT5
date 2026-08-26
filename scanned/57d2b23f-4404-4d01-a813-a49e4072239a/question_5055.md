# Q5055: promise API index validation — bls12381.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, promise indices that are out of range, freshly reused, or referencing a promise already resolved, with the input length one byte past the accepted maximum, and additionally with a (ptr,len) pair whose sum overflows the address space, reach `read_fp2_point` in `runtime/near-vm-runner/src/logic/bls12381.rs` and act on a promise the receipt does not own, breaking the invariant that promise indices are validated against the receipt's own promise set, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/near-vm-runner/src/logic/bls12381.rs` :: `read_fp2_point`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: promise indices that are out of range, freshly reused, or referencing a promise already resolved; with the input length one byte past the accepted maximum; with a (ptr,len) pair whose sum overflows the address space
- Exploit idea: act on a promise the receipt does not own
- Invariant to test: promise indices are validated against the receipt's own promise set
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test over invalid promise indices
