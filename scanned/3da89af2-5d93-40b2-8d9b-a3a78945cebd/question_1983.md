# Q1983: amounts - Amounts::add/sub cleanup drops a non-zero entry (3)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit, exploit the `DefaultMap` cleanup behaviour behind `checked_apply` in `contracts/defuse/core/src/amounts.rs` so an entry that reaches zero is removed while a paired entry is not, leaving the matcher's view of a token inconsistent with the account's stored balance, breaking the invariant `the matcher's accumulated delta for (account, token) == the change actually applied to that account's `token_balances`` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/amounts.rs](contracts/defuse/core/src/amounts.rs) - `checked_apply` (cross-check `amount_for` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit
- Attacker controls: the deposited amount, the nested batch, and `refund_if_fails`
- Exploit idea: Probe the interaction between `entry_or_default`, the zero-value cleanup, and iteration order during `finalize`. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: the matcher's accumulated delta for (account, token) == the change actually applied to that account's `token_balances`
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Property-test `Amounts` add/sub sequences returning to zero; assert map contents and reported balance agree.
