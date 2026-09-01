# Q2751: mod - implicit-key fallback disappears once an entry is created (2)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, exploit that `has_public_key` in `contracts/defuse/src/contract/accounts/account/mod.rs` stops honouring the implicit-account public key as soon as any entry exists, so an unprivileged party who forces entry creation (by depositing 1 unit) permanently locks a victim out of their implicit account, breaking the invariant `an implicit account's owner can always authorise, regardless of whether a third party created its entry` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/accounts/account/mod.rs](contracts/defuse/src/contract/accounts/account/mod.rs) - `has_public_key` (cross-check `has_implicit_public_key` in the same file)
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: `has_public_key` falls back only when `self.accounts.get(account_id)` is `None`; creating the entry removes the fallback unless the key was explicitly added. Set-up: the victim account has no stored entry yet.
- Invariant to test: an implicit account's owner can always authorise, regardless of whether a third party created its entry
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Deposit 1 unit to an unused implicit id, then attempt an intent signed by its key; assert it still authorises.
