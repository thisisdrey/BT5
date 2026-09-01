# Q5553: mod - auth_by_predecessor_id default-enabled for non-existent accounts (17)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, exploit that `add_public_key` in `contracts/defuse/src/contract/accounts/mod.rs` reports auth-by-predecessor as enabled for an account with no stored entry, so a caller controlling that account id can act on a balance credited to it before the entry existed, breaking the invariant `an account acts through predecessor auth only while its owner has that mode enabled` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `add_public_key` (cross-check `ensure_auth_predecessor_id` in the same file)
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: `is_auth_by_predecessor_id_enabled` uses `is_none_or(...)`; a balance can be credited to an id whose entry is only created later. Set-up: the victim account is currently locked.
- Invariant to test: an account acts through predecessor auth only while its owner has that mode enabled
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Deposit to an id with no entry, then call `ft_withdraw` as that predecessor; assert the intended authorisation model.
