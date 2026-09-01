# Q5851: mod - auth_by_predecessor_id default-enabled for non-existent accounts (25)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, exploit that `has_implicit_public_key` in `contracts/defuse/src/contract/accounts/account/mod.rs` reports auth-by-predecessor as enabled for an account with no stored entry, so a caller controlling that account id can act on a balance credited to it before the entry existed, breaking the invariant `an account acts through predecessor auth only while its owner has that mode enabled` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/mod.rs](contracts/defuse/src/contract/accounts/account/mod.rs) - `has_implicit_public_key` (cross-check `set_implicit_public_key_removed` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: `is_auth_by_predecessor_id_enabled` uses `is_none_or(...)`; a balance can be credited to an id whose entry is only created later. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: an account acts through predecessor auth only while its owner has that mode enabled
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deposit to an id with no entry, then call `ft_withdraw` as that predecessor; assert the intended authorisation model.
