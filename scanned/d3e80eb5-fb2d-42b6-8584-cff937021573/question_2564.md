# Q2564: contract code cache keyed on something weaker than the code hash — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployContract` action carrying attacker-authored WASM, two contracts whose compiled artifacts share a cache key derived from less than the full code hash plus config, when combined with a DeployContract earlier in the same action list, reach `record_storage_usage` in `runtime/runtime/src/universal_account_id.rs` and make one account execute another account's compiled code, breaking the invariant that the compiled-artifact cache key commits to the code hash, VM kind, and full VM config, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/universal_account_id.rs` :: `record_storage_usage`
- Entrypoint: a `DeployContract` action carrying attacker-authored WASM
- Attacker controls: two contracts whose compiled artifacts share a cache key derived from less than the full code hash plus config; when combined with a DeployContract earlier in the same action list
- Exploit idea: make one account execute another account's compiled code
- Invariant to test: the compiled-artifact cache key commits to the code hash, VM kind, and full VM config
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test asserting cache-key inputs include every semantics-affecting field
