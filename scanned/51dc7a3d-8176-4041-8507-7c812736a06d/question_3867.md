# Q3867: salt_registry - legacy vs new nonce map split allows one nonce twice (10)

## Question
Given the payload `deadline` is far in the future while the nonce's own deadline is near, can an unprivileged attacker, entering through re-submission of a payload the attacker observed on-chain or in a public relayer mempool, commit a nonce through `derive_next_salt` in `contracts/defuse/src/contract/state/salt_registry.rs` that is present in neither map's checked range, exploiting that `commit` only rejects legacy hits but writes solely to the new map, breaking the invariant ``is_used(n)` after `commit(n)` == true, for every `n` and every map configuration` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/src/contract/state/salt_registry.rs](contracts/defuse/src/contract/state/salt_registry.rs) - `derive_next_salt` (cross-check `SaltRegistry` in the same file)
- Entrypoint: re-submission of a payload the attacker observed on-chain or in a public relayer mempool
- Attacker controls: when and how many times the observed payload is replayed, and the block timestamp it lands in
- Exploit idea: Probe the boundary where a nonce is considered by `is_used` on one map but committed to the other, especially across a state migration. Set-up: the payload `deadline` is far in the future while the nonce's own deadline is near.
- Invariant to test: `is_used(n)` after `commit(n)` == true, for every `n` and every map configuration
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `MaybeLegacyNonces` with overlapping legacy/new contents; assert commit-then-is_used holds.
