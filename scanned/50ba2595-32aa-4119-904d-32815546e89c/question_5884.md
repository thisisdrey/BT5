# Q5884: deltas - HashMap iteration order changes the settlement (8)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, exploit that `Deltas` in `contracts/defuse/core/src/engine/state/deltas.rs` iterates a `HashMap` whose order is not fixed, so the sender/receiver pairing produced by `finalize_into` (and therefore the emitted `mt_transfer` attribution) depends on ordering the attacker can influence via chosen account ids, breaking the invariant `the set of `Transfers` produced for a given delta set == a deterministic function of that delta set` and leading to temporary freezing of user funds?

## Target
- File/function: [contracts/defuse/core/src/engine/state/deltas.rs](contracts/defuse/core/src/engine/state/deltas.rs) - `Deltas` (cross-check `ft_withdraw` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Deposits and withdrawals are sorted by amount with `sort_unstable_by_key(Reverse(amount))`; equal amounts leave ties broken by iteration order. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: the set of `Transfers` produced for a given delta set == a deterministic function of that delta set
- Expected Immunefi impact: High - Temporary freezing of user funds
- Fast validation: Run `finalize()` repeatedly on an equal-amount delta set across account-id permutations; assert identical `Transfers`.
