# Q3460: mod - deltas recorded on the matcher but not on storage (or the reverse) (7)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit, find an ordering through `verify_intent_nonce` in `contracts/defuse/core/src/engine/mod.rs` where `Deltas` records a movement that the underlying `State` rejected, or where the underlying state mutates without the matcher observing it, breaking the invariant `for every token, the matcher's net delta == the net change written to persistent `token_balances`` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/core/src/engine/mod.rs](contracts/defuse/core/src/engine/mod.rs) - `verify_intent_nonce` (cross-check `execute_signed_intents` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` with `execute_intents` funded by the attacker's own deposit
- Attacker controls: the deposited amount, the nested batch, and `refund_if_fails`
- Exploit idea: `internal_add_balance` writes to `self.state` first, then `self.deltas.deposit(...)`; probe whether a failure between them (or a `?` early return) leaves the two out of sync mid-batch. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: for every token, the matcher's net delta == the net change written to persistent `token_balances`
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Inject a failure between the state write and the matcher update; assert the batch aborts and no partial write survives.
