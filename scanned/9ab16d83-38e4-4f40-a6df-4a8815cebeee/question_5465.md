# Q5465: mod - CachedState divergence from live state (7)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit, make `ft_withdraw` in `contracts/defuse/core/src/engine/state/mod.rs` serve a cached value that differs from what the live `State` would return mid-batch, so an intent later in the batch is authorised against stale balances, keys or lock state, breaking the invariant `every read during a batch == the value the same read would return against live state at that point` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/engine/state/mod.rs](contracts/defuse/core/src/engine/state/mod.rs) - `ft_withdraw` (cross-check `internal_apply_deltas` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit
- Attacker controls: the deposited amount, the nested batch, and `refund_if_fails`
- Exploit idea: `CachedState` backs `simulate_intents`, and reads inside a batch may be served from the cache while writes go elsewhere. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: every read during a batch == the value the same read would return against live state at that point
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Sequence two intents where the second depends on the first's write; assert the cached read reflects it.
