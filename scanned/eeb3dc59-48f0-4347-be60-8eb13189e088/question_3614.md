# Q3614: mod - implicit-key fallback disappears once an entry is created (10)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, exploit that `ensure_auth_predecessor_id` in `contracts/defuse/src/contract/accounts/mod.rs` stops honouring the implicit-account public key as soon as any entry exists, so an unprivileged party who forces entry creation (by depositing 1 unit) permanently locks a victim out of their implicit account, breaking the invariant `an implicit account's owner can always authorise, regardless of whether a third party created its entry` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `ensure_auth_predecessor_id` (cross-check `get_or_create` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: `has_public_key` falls back only when `self.accounts.get(account_id)` is `None`; creating the entry removes the fallback unless the key was explicitly added. Set-up: the victim account has no stored entry yet.
- Invariant to test: an implicit account's owner can always authorise, regardless of whether a third party created its entry
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Deposit 1 unit to an unused implicit id, then attempt an intent signed by its key; assert it still authorises.
