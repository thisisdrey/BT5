# Q4938: simulate - event emission carries an attacker-chosen account attribution (13)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, make `SimulateInspector` in `contracts/defuse/src/contract/intents/simulate.rs` emit an `AccountEvent`/`MtTransferEvent` attributing a movement to an account that did not authorise it, so downstream automation (solvers, relayers, indexers) acts on a false attribution, breaking the invariant `the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [contracts/defuse/src/contract/intents/simulate.rs](contracts/defuse/src/contract/intents/simulate.rs) - `SimulateInspector` (cross-check `on_intent_executed` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Attribution is derived from the matcher's pairing, not from the signer; the code notes the `old_owner_id` can be a locked account. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Craft a diff whose matcher pairing attributes a transfer to an uninvolved account; assert attribution correctness.
