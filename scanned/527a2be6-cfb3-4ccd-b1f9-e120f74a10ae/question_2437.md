# Q2437: promise_yield data-id collision — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through attacker WASM calling `promise_yield_create` / `promise_yield_resume`, two yields whose data ids are derived from attacker-chosen inputs that collide, when combined with a DeployContract earlier in the same action list, reach `use_global_contract` in `runtime/runtime/src/global_contracts.rs` and resume another contract's yielded promise with attacker-supplied payload, breaking the invariant that yield data ids are unforgeable and bound to the creating receipt, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `use_global_contract`
- Entrypoint: attacker WASM calling `promise_yield_create` / `promise_yield_resume`
- Attacker controls: two yields whose data ids are derived from attacker-chosen inputs that collide; when combined with a DeployContract earlier in the same action list
- Exploit idea: resume another contract's yielded promise with attacker-supplied payload
- Invariant to test: yield data ids are unforgeable and bound to the creating receipt
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test on data-id derivation asserting unforgeability
