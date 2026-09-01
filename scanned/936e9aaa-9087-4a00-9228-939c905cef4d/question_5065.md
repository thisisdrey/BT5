# Q5065: state - refund_if_fails changes atomicity in the attacker's favour

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, exploit the `refund_if_fails` branch reached from `auth_call` in `contracts/defuse/src/contract/intents/state.rs`, which either calls `execute_intents` inline or schedules it as a detached promise, so a failure refunds the deposit while the intents still take effect, breaking the invariant `a deposit is refunded only when the intents it funded had no effect` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/intents/state.rs](contracts/defuse/src/contract/intents/state.rs) - `auth_call` (cross-check `ft_withdraw` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: Inline execution shares the receipt; the detached `ext_intents::...detach()` path does not, so failure semantics differ between the two. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: a deposit is refunded only when the intents it funded had no effect
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox both branches with a failing intent; assert deposit and effect are all-or-nothing.
