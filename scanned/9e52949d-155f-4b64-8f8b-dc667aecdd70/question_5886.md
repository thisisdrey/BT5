# Q5886: account - event emission carries an attacker-chosen account attribution (14)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through `execute_intents` mixing payloads from several signers in one vector, make `SetAuthByPredecessorId` in `contracts/defuse/core/src/intents/account.rs` emit an `AccountEvent`/`MtTransferEvent` attributing a movement to an account that did not authorise it, so downstream automation (solvers, relayers, indexers) acts on a false attribution, breaking the invariant `the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [contracts/defuse/core/src/intents/account.rs](contracts/defuse/core/src/intents/account.rs) - `SetAuthByPredecessorId` (cross-check `execute_intent` in the same file)
- Entrypoint: `execute_intents` mixing payloads from several signers in one vector
- Attacker controls: the number and order of payloads and which accounts each targets
- Exploit idea: Attribution is derived from the matcher's pairing, not from the signer; the code notes the `old_owner_id` can be a locked account. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Craft a diff whose matcher pairing attributes a transfer to an uninvolved account; assert attribution correctness.
