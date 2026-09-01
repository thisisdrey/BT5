# Q1435: simulate - intent batch mixes accounts so one signer's guard protects another (3)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `execute_intents` mixing payloads from several signers in one vector, submit a batch where `SimulateInspector` in `contracts/defuse/src/contract/intents/simulate.rs` applies a check (lock, nonce, key, auth mode) resolved for one `signer_id` to an effect landing on a different account, breaking the invariant `the account each check is resolved against == the account the corresponding effect lands on` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/intents/simulate.rs](contracts/defuse/src/contract/intents/simulate.rs) - `SimulateInspector` (cross-check `on_intent_executed` in the same file)
- Entrypoint: `execute_intents` mixing payloads from several signers in one vector
- Attacker controls: the number and order of payloads and which accounts each targets
- Exploit idea: Each `MultiPayload` in the vector carries its own `signer_id`; probe every place the engine reuses a value across iterations of the batch loop. Set-up: the victim account has no stored entry yet.
- Invariant to test: the account each check is resolved against == the account the corresponding effect lands on
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Batch two payloads with different signers where the second targets the first's balance; assert per-signer isolation.
