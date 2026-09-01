# Q5059: mod - event emission carries an attacker-chosen account attribution (3)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, make `ensure_auth_predecessor_id` in `contracts/defuse/src/contract/accounts/mod.rs` emit an `AccountEvent`/`MtTransferEvent` attributing a movement to an account that did not authorise it, so downstream automation (solvers, relayers, indexers) acts on a false attribution, breaking the invariant `the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [contracts/defuse/src/contract/accounts/mod.rs](contracts/defuse/src/contract/accounts/mod.rs) - `ensure_auth_predecessor_id` (cross-check `set_auth_by_predecessor_id_and_emit_event` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Attribution is derived from the matcher's pairing, not from the signer; the code notes the `old_owner_id` can be a locked account. Set-up: the victim account has no stored entry yet.
- Invariant to test: the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Craft a diff whose matcher pairing attributes a transfer to an uninvolved account; assert attribution correctness.
