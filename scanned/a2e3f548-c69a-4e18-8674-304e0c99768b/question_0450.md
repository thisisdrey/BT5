# Q0450: salt_registry - legacy vs new nonce map split allows one nonce twice (3)

## Question
Given the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, commit a nonce through `SaltRegistry` in `contracts/defuse/src/contract/state/salt_registry.rs` that is present in neither map's checked range, exploiting that `commit` only rejects legacy hits but writes solely to the new map, breaking the invariant ``is_used(n)` after `commit(n)` == true, for every `n` and every map configuration` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/src/contract/state/salt_registry.rs](contracts/defuse/src/contract/state/salt_registry.rs) - `SaltRegistry` (cross-check `invalidate` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: Probe the boundary where a nonce is considered by `is_used` on one map but committed to the other, especially across a state migration. Set-up: the nonce word for the target 248-bit prefix already carries a bit committed by an earlier intent.
- Invariant to test: `is_used(n)` after `commit(n)` == true, for every `n` and every map configuration
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `MaybeLegacyNonces` with overlapping legacy/new contents; assert commit-then-is_used holds.
