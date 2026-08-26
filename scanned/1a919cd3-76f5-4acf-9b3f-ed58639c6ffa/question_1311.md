# Q1311: ecrecover / signature host malleability — gas_counter.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, high-s secp256k1 signatures, v values outside the canonical range, and zero r or s, with the input length at exactly the host function's accepted maximum, reach `update_profile_host` in `runtime/near-vm-runner/src/logic/gas_counter.rs` and recover a key from a malleable signature so contract-level authorisation is bypassed, breaking the invariant that signature host functions reject non-canonical inputs deterministically, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-vm-runner/src/logic/gas_counter.rs` :: `update_profile_host`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: high-s secp256k1 signatures, v values outside the canonical range, and zero r or s; with the input length at exactly the host function's accepted maximum
- Exploit idea: recover a key from a malleable signature so contract-level authorisation is bypassed
- Invariant to test: signature host functions reject non-canonical inputs deterministically
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over malleable signature inputs
