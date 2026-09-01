# Q5676: execute - event emission carries an attacker-chosen account attribution (16)

## Question
Given the attacker deposited 1 unit to force the victim's entry into existence, can an unprivileged attacker, entering through `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth), make `on_intent_executed` in `contracts/defuse/src/contract/intents/execute.rs` emit an `AccountEvent`/`MtTransferEvent` attributing a movement to an account that did not authorise it, so downstream automation (solvers, relayers, indexers) acts on a false attribution, breaking the invariant `the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [contracts/defuse/src/contract/intents/execute.rs](contracts/defuse/src/contract/intents/execute.rs) - `on_intent_executed` (cross-check `ExecuteInspector` in the same file)
- Entrypoint: `add_public_key` / `remove_public_key` / `disable_auth_by_predecessor_id` called directly (1 yocto, predecessor auth)
- Attacker controls: the `public_key` argument and the calling account id
- Exploit idea: Attribution is derived from the matcher's pairing, not from the signer; the code notes the `old_owner_id` can be a locked account. Set-up: the attacker deposited 1 unit to force the victim's entry into existence.
- Invariant to test: the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Craft a diff whose matcher pairing attributes a transfer to an uninvolved account; assert attribution correctness.
