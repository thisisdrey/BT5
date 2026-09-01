# Q2912: mod - state version / config read before initialisation (12)

## Question
Given the victim account is currently locked, can an unprivileged attacker, entering through `simulate_intents` as a probe of another account's state before acting, reach `ContractStorage` in `contracts/defuse/src/contract/mod.rs` before the corresponding state field is initialised, so a default `wnear_id`, `fee_collector` or `Pips` value is used for a real settlement, breaking the invariant `every settlement uses the configured `wnear_id`/`fee_collector`, never a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/mod.rs](contracts/defuse/src/contract/mod.rs) - `ContractStorage` (cross-check `Role` in the same file)
- Entrypoint: `simulate_intents` as a probe of another account's state before acting
- Attacker controls: the probe batch composition
- Exploit idea: Probe every unprivileged entrypoint reachable in the window between deployment and full configuration, and every `unwrap_or_default()` on a config read. Set-up: the victim account is currently locked.
- Invariant to test: every settlement uses the configured `wnear_id`/`fee_collector`, never a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call each unprivileged entrypoint against a partially-initialised contract; assert rejection.
