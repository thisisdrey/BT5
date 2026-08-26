# Q3327: global contract reference-count accounting — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, many accounts adopting and abandoning the same global contract across shards in one block, when combined with a DeployContract earlier in the same action list, and additionally when combined with a DeleteAccount later in the same action list, reach `decode_symbol` in `core/primitives-core/src/universal_account_id.rs` and make the reference count underflow so live code is garbage-collected, or overflow so storage is never reclaimed, breaking the invariant that the global contract reference count exactly matches the set of accounts pointing at it, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/primitives-core/src/universal_account_id.rs` :: `decode_symbol`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: many accounts adopting and abandoning the same global contract across shards in one block; when combined with a DeployContract earlier in the same action list; when combined with a DeleteAccount later in the same action list
- Exploit idea: make the reference count underflow so live code is garbage-collected, or overflow so storage is never reclaimed
- Invariant to test: the global contract reference count exactly matches the set of accounts pointing at it
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: store test asserting refcount equals the account set after churn
