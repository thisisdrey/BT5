# Q1630: simulate - intent batch mixes accounts so one signer's guard protects another (4)

## Question
Given the victim account has no stored entry yet, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, submit a batch where `on_intent_executed` in `contracts/defuse/src/contract/intents/simulate.rs` applies a check (lock, nonce, key, auth mode) resolved for one `signer_id` to an effect landing on a different account, breaking the invariant `the account each check is resolved against == the account the corresponding effect lands on` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/intents/simulate.rs](contracts/defuse/src/contract/intents/simulate.rs) - `on_intent_executed` (cross-check `SimulateInspector` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: Each `MultiPayload` in the vector carries its own `signer_id`; probe every place the engine reuses a value across iterations of the batch loop. Set-up: the victim account has no stored entry yet.
- Invariant to test: the account each check is resolved against == the account the corresponding effect lands on
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Batch two payloads with different signers where the second targets the first's balance; assert per-signer isolation.
