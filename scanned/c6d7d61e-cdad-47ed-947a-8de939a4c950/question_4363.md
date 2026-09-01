# Q4363: state - refund from a locked account bypasses lock semantics

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, use the documented allowance in `auth_call` of `contracts/defuse/src/contract/intents/state.rs` that refunds may move funds out of a locked receiver, to extract value from an account the protocol intended to freeze, breaking the invariant `assets leaving a locked account == refunds of transfers that account itself received in the same flow` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/intents/state.rs](contracts/defuse/src/contract/intents/state.rs) - `auth_call` (cross-check `notify_on_transfer` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: The resolver deliberately uses `as_inner_unchecked_mut()`; probe whether an attacker can lock/trigger the sequence to drain a frozen account, or make a lock ineffective. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: assets leaving a locked account == refunds of transfers that account itself received in the same flow
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Lock a receiver between transfer and resolve; assert only the in-flight amount can leave.
