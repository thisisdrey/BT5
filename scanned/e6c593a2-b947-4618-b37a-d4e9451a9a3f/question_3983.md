# Q3983: mod - ensure_auth_predecessor_id on a sub-account or aliased id (3)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, call `ensure_auth_predecessor_id` in `contracts/defuse/src/contract/accounts/mod.rs` from an account id that the contract treats as equivalent to a victim's (casing, implicit-vs-named form, sub-account) so predecessor authorisation applies to the wrong balance, breaking the invariant `the balance key derived from the predecessor == the key the depositor's funds were credited under` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `ensure_auth_predecessor_id` (cross-check `has_public_key` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: `env::predecessor_account_id()` is used directly as the balance key; any normalisation difference between the key used at deposit time and at withdrawal time is exploitable. Set-up: the victim account has no stored entry yet.
- Invariant to test: the balance key derived from the predecessor == the key the depositor's funds were credited under
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deposit under one id form and withdraw under another; assert the contract treats them as one account or rejects.
