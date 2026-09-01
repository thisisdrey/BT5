# Q4015: accounts - event emission carries an attacker-chosen account attribution (12)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, make `AccountEvent` in `contracts/defuse/core/src/accounts.rs` emit an `AccountEvent`/`MtTransferEvent` attributing a movement to an account that did not authorise it, so downstream automation (solvers, relayers, indexers) acts on a false attribution, breaking the invariant `the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [contracts/defuse/core/src/accounts.rs](contracts/defuse/core/src/accounts.rs) - `AccountEvent` (cross-check `PublicKeyEvent` in the same file)
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: Attribution is derived from the matcher's pairing, not from the signer; the code notes the `old_owner_id` can be a locked account. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: the `old_owner_id`/`account_id` in an emitted event == the account whose signed intent caused the movement
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Craft a diff whose matcher pairing attributes a transfer to an uninvolved account; assert attribution correctness.
