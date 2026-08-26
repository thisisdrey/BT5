# Q5894: ecrecover / signature host malleability — dependencies.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling the host function directly inside a `FunctionCall`, high-s secp256k1 signatures, v values outside the canonical range, and zero r or s, with a (ptr,len) pair whose sum overflows the address space, and additionally with a zero-length access at the last valid memory page, reach `view_memory` in `runtime/near-vm-runner/src/logic/dependencies.rs` and recover a key from a malleable signature so contract-level authorisation is bypassed, breaking the invariant that signature host functions reject non-canonical inputs deterministically, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-vm-runner/src/logic/dependencies.rs` :: `view_memory`
- Entrypoint: attacker WASM calling the host function directly inside a `FunctionCall`
- Attacker controls: high-s secp256k1 signatures, v values outside the canonical range, and zero r or s; with a (ptr,len) pair whose sum overflows the address space; with a zero-length access at the last valid memory page
- Exploit idea: recover a key from a malleable signature so contract-level authorisation is bypassed
- Invariant to test: signature host functions reject non-canonical inputs deterministically
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over malleable signature inputs
