# Q0536: accounts - auth_by_predecessor_id default-enabled for non-existent accounts (3)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, exploit that `disable_auth_by_predecessor_id` in `contracts/defuse/src/accounts.rs` reports auth-by-predecessor as enabled for an account with no stored entry, so a caller controlling that account id can act on a balance credited to it before the entry existed, breaking the invariant `an account acts through predecessor auth only while its owner has that mode enabled` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/accounts.rs](contracts/defuse/src/accounts.rs) - `disable_auth_by_predecessor_id` (cross-check `add_public_key` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: `is_auth_by_predecessor_id_enabled` uses `is_none_or(...)`; a balance can be credited to an id whose entry is only created later. Set-up: the victim account has no stored entry yet.
- Invariant to test: an account acts through predecessor auth only while its owner has that mode enabled
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deposit to an id with no entry, then call `ft_withdraw` as that predecessor; assert the intended authorisation model.
