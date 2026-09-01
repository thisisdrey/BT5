# Q3122: mod - implicit-key fallback disappears once an entry is created (6)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth), exploit that `Accounts` in `contracts/defuse/src/contract/accounts/mod.rs` stops honouring the implicit-account public key as soon as any entry exists, so an unprivileged party who forces entry creation (by depositing 1 unit) permanently locks a victim out of their implicit account, breaking the invariant `an implicit account's owner can always authorise, regardless of whether a third party created its entry` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `Accounts` (cross-check `get_mut` in the same file)
- Entrypoint: `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth)
- Attacker controls: the `public_key` argument and the calling account id
- Exploit idea: `has_public_key` falls back only when `self.accounts.get(account_id)` is `None`; creating the entry removes the fallback unless the key was explicitly added. Set-up: the victim account has no stored entry yet.
- Invariant to test: an implicit account's owner can always authorise, regardless of whether a third party created its entry
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Deposit 1 unit to an unused implicit id, then attempt an intent signed by its key; assert it still authorises.
