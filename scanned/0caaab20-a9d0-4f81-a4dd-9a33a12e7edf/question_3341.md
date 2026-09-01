# Q3341: account - account entry version migration changes stored balances or keys (8)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, trigger the entry-version path in `RemovePublicKey` of `contracts/defuse/core/src/intents/account.rs` (v0 -> v1) from an unprivileged entrypoint so a balance, nonce bitmap or public-key set is read under the wrong layout, breaking the invariant `an account's balances and keys after a version transition == the values before it` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/intents/account.rs](contracts/defuse/core/src/intents/account.rs) - `RemovePublicKey` (cross-check `SetAuthByPredecessorId` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Probe which unprivileged call first materialises or upgrades an entry and whether the two layouts agree on every field. Set-up: the victim account is currently locked.
- Invariant to test: an account's balances and keys after a version transition == the values before it
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Create a v0 entry, trigger the upgrade path, and assert every field is preserved.
