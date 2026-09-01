# Q1043: execute - intent batch mixes accounts so one signer's guard protects another

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`, submit a batch where `ExecuteInspector` in `contracts/defuse/src/contract/intents/execute.rs` applies a check (lock, nonce, key, auth mode) resolved for one `signer_id` to an effect landing on a different account, breaking the invariant `the account each check is resolved against == the account the corresponding effect lands on` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/intents/execute.rs](contracts/defuse/src/contract/intents/execute.rs) - `ExecuteInspector` (cross-check `on_intent_executed` in the same file)
- Entrypoint: an `AddPublicKey` / `RemovePublicKey` / `SetAuthByPredecessorId` intent inside `execute_intents`
- Attacker controls: the key bytes and the position of the intent within the batch
- Exploit idea: Each `MultiPayload` in the vector carries its own `signer_id`; probe every place the engine reuses a value across iterations of the batch loop. Set-up: the victim account has no stored entry yet.
- Invariant to test: the account each check is resolved against == the account the corresponding effect lands on
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Batch two payloads with different signers where the second targets the first's balance; assert per-signer isolation.
