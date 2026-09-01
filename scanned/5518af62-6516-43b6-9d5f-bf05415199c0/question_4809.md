# Q4809: mod - intent batch mixes accounts so one signer's guard protects another (14)

## Question
Given the attacker deposited 1 unit to force the victim's entry into existence, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, submit a batch where `execute_intents` in `contracts/defuse/src/contract/intents/mod.rs` applies a check (lock, nonce, key, auth mode) resolved for one `signer_id` to an effect landing on a different account, breaking the invariant `the account each check is resolved against == the account the corresponding effect lands on` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/intents/mod.rs](contracts/defuse/src/contract/intents/mod.rs) - `execute_intents` (cross-check `simulate_intents` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Each `MultiPayload` in the vector carries its own `signer_id`; probe every place the engine reuses a value across iterations of the batch loop. Set-up: the attacker deposited 1 unit to force the victim's entry into existence.
- Invariant to test: the account each check is resolved against == the account the corresponding effect lands on
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Batch two payloads with different signers where the second targets the first's balance; assert per-signer isolation.
