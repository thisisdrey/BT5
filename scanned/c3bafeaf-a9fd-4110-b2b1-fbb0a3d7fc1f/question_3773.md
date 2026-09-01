# Q3773: mod - state version / config read before initialisation (14)

## Question
Given the victim's entry is still at the v0 layout, can an unprivileged attacker, entering through `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry, reach `Contract` in `contracts/defuse/src/contract/mod.rs` before the corresponding state field is initialised, so a default `wnear_id`, `fee_collector` or `Pips` value is used for a real settlement, breaking the invariant `every settlement uses the configured `wnear_id`/`fee_collector`, never a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/mod.rs](contracts/defuse/src/contract/mod.rs) - `Contract` (cross-check `migrate` in the same file)
- Entrypoint: `ft_on_transfer` with a `msg` naming any `receiver_id`, which force-creates that account entry
- Attacker controls: the target `receiver_id` and the (possibly minimal) deposited amount
- Exploit idea: Probe every unprivileged entrypoint reachable in the window between deployment and full configuration, and every `unwrap_or_default()` on a config read. Set-up: the victim's entry is still at the v0 layout.
- Invariant to test: every settlement uses the configured `wnear_id`/`fee_collector`, never a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Call each unprivileged entrypoint against a partially-initialised contract; assert rejection.
