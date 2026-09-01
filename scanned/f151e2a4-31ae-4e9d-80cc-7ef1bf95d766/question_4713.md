# Q4713: mod - Amounts::add/sub cleanup drops a non-zero entry (11)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit, exploit the `DefaultMap` cleanup behaviour behind `ft_withdraw` in `contracts/defuse/core/src/engine/state/mod.rs` so an entry that reaches zero is removed while a paired entry is not, leaving the matcher's view of a token inconsistent with the account's stored balance, breaking the invariant `the matcher's accumulated delta for (account, token) == the change actually applied to that account's `token_balances`` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/engine/state/mod.rs](contracts/defuse/core/src/engine/state/mod.rs) - `ft_withdraw` (cross-check `notify_on_transfer` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit
- Attacker controls: the deposited amount, the nested batch, and `refund_if_fails`
- Exploit idea: Probe the interaction between `entry_or_default`, the zero-value cleanup, and iteration order during `finalize`. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: the matcher's accumulated delta for (account, token) == the change actually applied to that account's `token_balances`
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Property-test `Amounts` add/sub sequences returning to zero; assert map contents and reported balance agree.
