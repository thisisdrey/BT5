# Q1430: mod - auth_by_predecessor_id default-enabled for non-existent accounts (9)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `execute_intents` mixing payloads from several signers in one vector, exploit that `ensure_auth_predecessor_id` in `contracts/defuse/src/contract/accounts/mod.rs` reports auth-by-predecessor as enabled for an account with no stored entry, so a caller controlling that account id can act on a balance credited to it before the entry existed, breaking the invariant `an account acts through predecessor auth only while its owner has that mode enabled` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `ensure_auth_predecessor_id` (cross-check `remove_public_key` in the same file)
- Entrypoint: `execute_intents` mixing payloads from several signers in one vector
- Attacker controls: the number and order of payloads and which accounts each targets
- Exploit idea: `is_auth_by_predecessor_id_enabled` uses `is_none_or(...)`; a balance can be credited to an id whose entry is only created later. Set-up: the victim account has no stored entry yet.
- Invariant to test: an account acts through predecessor auth only while its owner has that mode enabled
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deposit to an id with no entry, then call `ft_withdraw` as that predecessor; assert the intended authorisation model.
