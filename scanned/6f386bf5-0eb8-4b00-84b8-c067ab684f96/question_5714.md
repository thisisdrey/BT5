# Q5714: mod - legacy vs new nonce map split allows one nonce twice (13)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account, commit a nonce through `cleanup_by_prefix` in `contracts/defuse/core/src/nonce/mod.rs` that is present in neither map's checked range, exploiting that `commit` only rejects legacy hits but writes solely to the new map, breaking the invariant ``is_used(n)` after `commit(n)` == true, for every `n` and every map configuration` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/nonce/mod.rs](contracts/defuse/core/src/nonce/mod.rs) - `cleanup_by_prefix` (cross-check `commit` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` submitted repeatedly by any account
- Attacker controls: the 32-byte `nonce`, the `deadline`, the salt bytes embedded in a versioned nonce, and submission timing
- Exploit idea: Probe the boundary where a nonce is considered by `is_used` on one map but committed to the other, especially across a state migration. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: `is_used(n)` after `commit(n)` == true, for every `n` and every map configuration
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `MaybeLegacyNonces` with overlapping legacy/new contents; assert commit-then-is_used holds.
