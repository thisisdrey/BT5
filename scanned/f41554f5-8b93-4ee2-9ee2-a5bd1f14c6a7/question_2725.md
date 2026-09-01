# Q2725: mod - zero-amount / empty-collection guard bypass (3)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit, reach `internal_sub_balance` in `contracts/defuse/core/src/engine/state/mod.rs` with an amount of zero, an empty `Amounts`, or an empty `diff` through a path that does not run the `InvalidIntent` guard, so an event is emitted or state touched with no corresponding value, breaking the invariant `every balance-mutating call carries a strictly positive amount` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/core/src/engine/state/mod.rs](contracts/defuse/core/src/engine/state/mod.rs) - `internal_sub_balance` (cross-check `notify_on_transfer` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit
- Attacker controls: the deposited amount, the nested batch, and `refund_if_fails`
- Exploit idea: Guards live in some callers but not all; enumerate every path into `internal_add_balance` / `internal_sub_balance` / `deposit` / `withdraw`. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: every balance-mutating call carries a strictly positive amount
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Enumerate callers of `internal_sub_balance`; assert each rejects zero amounts.
