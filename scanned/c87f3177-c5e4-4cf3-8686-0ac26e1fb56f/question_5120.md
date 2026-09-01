# Q5120: nonces - legacy vs new nonce map split allows one nonce twice (15)

## Question
Given the account still carries a legacy (pre-versioned) nonce map alongside the new one, can an unprivileged attacker, entering through `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits, commit a nonce through `MaybeLegacyNonces` in `contracts/defuse/src/contract/accounts/account/nonces.rs` that is present in neither map's checked range, exploiting that `commit` only rejects legacy hits but writes solely to the new map, breaking the invariant ``is_used(n)` after `commit(n)` == true, for every `n` and every map configuration` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/nonces.rs](contracts/defuse/src/contract/accounts/account/nonces.rs) - `MaybeLegacyNonces` (cross-check `is_used` in the same file)
- Entrypoint: `ft_on_transfer` `msg` with `execute_intents`, replayed across separate deposits
- Attacker controls: the nonce and deadline of each nested payload, plus the number of deposits
- Exploit idea: Probe the boundary where a nonce is considered by `is_used` on one map but committed to the other, especially across a state migration. Set-up: the account still carries a legacy (pre-versioned) nonce map alongside the new one.
- Invariant to test: `is_used(n)` after `commit(n)` == true, for every `n` and every map configuration
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `MaybeLegacyNonces` with overlapping legacy/new contents; assert commit-then-is_used holds.
