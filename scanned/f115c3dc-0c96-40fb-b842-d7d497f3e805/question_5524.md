# Q5524: deltas - Amounts::add/sub cleanup drops a non-zero entry (6)

## Question
Given the attacker signs both counterparties using accounts they control, can an unprivileged attacker, entering through `execute_intents` where the attacker signs both sides of a trade using accounts they control, exploit the `DefaultMap` cleanup behaviour behind `Deltas` in `contracts/defuse/core/src/engine/state/deltas.rs` so an entry that reaches zero is removed while a paired entry is not, leaving the matcher's view of a token inconsistent with the account's stored balance, breaking the invariant `the matcher's accumulated delta for (account, token) == the change actually applied to that account's `token_balances`` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/engine/state/deltas.rs](contracts/defuse/core/src/engine/state/deltas.rs) - `Deltas` (cross-check `deposit` in the same file)
- Entrypoint: `execute_intents` where the attacker signs both sides of a trade using accounts they control
- Attacker controls: both sides' deltas, account ids, and the order of payloads in the vector
- Exploit idea: Probe the interaction between `entry_or_default`, the zero-value cleanup, and iteration order during `finalize`. Set-up: the attacker signs both counterparties using accounts they control.
- Invariant to test: the matcher's accumulated delta for (account, token) == the change actually applied to that account's `token_balances`
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Property-test `Amounts` add/sub sequences returning to zero; assert map contents and reported balance agree.
