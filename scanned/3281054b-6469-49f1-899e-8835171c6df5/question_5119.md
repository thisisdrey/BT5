# Q5119: mod - implicit-key fallback disappears once an entry is created (13)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, exploit that `AccountPrefix` in `contracts/defuse/src/contract/accounts/account/mod.rs` stops honouring the implicit-account public key as soon as any entry exists, so an unprivileged party who forces entry creation (by depositing 1 unit) permanently locks a victim out of their implicit account, breaking the invariant `an implicit account's owner can always authorise, regardless of whether a third party created its entry` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/mod.rs](contracts/defuse/src/contract/accounts/account/mod.rs) - `AccountPrefix` (cross-check `is_auth_by_predecessor_id_enabled` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: `has_public_key` falls back only when `self.accounts.get(account_id)` is `None`; creating the entry removes the fallback unless the key was explicitly added. Set-up: the victim account is currently locked.
- Invariant to test: an implicit account's owner can always authorise, regardless of whether a third party created its entry
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Deposit 1 unit to an unused implicit id, then attempt an intent signed by its key; assert it still authorises.
