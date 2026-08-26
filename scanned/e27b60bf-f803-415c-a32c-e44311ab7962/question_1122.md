# Q1122: global contract reference-count accounting — universal_state_init.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, many accounts adopting and abandoning the same global contract across shards in one block, when combined with a DeployContract earlier in the same action list, reach `from_raw` in `core/primitives/src/universal_state_init.rs` and make the reference count underflow so live code is garbage-collected, or overflow so storage is never reclaimed, breaking the invariant that the global contract reference count exactly matches the set of accounts pointing at it, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/primitives/src/universal_state_init.rs` :: `from_raw`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: many accounts adopting and abandoning the same global contract across shards in one block; when combined with a DeployContract earlier in the same action list
- Exploit idea: make the reference count underflow so live code is garbage-collected, or overflow so storage is never reclaimed
- Invariant to test: the global contract reference count exactly matches the set of accounts pointing at it
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: store test asserting refcount equals the account set after churn
