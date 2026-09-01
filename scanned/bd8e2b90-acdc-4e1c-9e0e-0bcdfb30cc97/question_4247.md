# Q4247: resolver - refund from a locked account bypasses lock semantics (4)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, use the documented allowance in `mt_resolve_transfer` of `contracts/defuse/src/contract/tokens/nep245/resolver.rs` that refunds may move funds out of a locked receiver, to extract value from an account the protocol intended to freeze, breaking the invariant `assets leaving a locked account == refunds of transfers that account itself received in the same flow` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/resolver.rs](contracts/defuse/src/contract/tokens/nep245/resolver.rs) - `mt_resolve_transfer`
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: The resolver deliberately uses `as_inner_unchecked_mut()`; probe whether an attacker can lock/trigger the sequence to drain a frozen account, or make a lock ineffective. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: assets leaving a locked account == refunds of transfers that account itself received in the same flow
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Lock a receiver between transfer and resolve; assert only the in-flight amount can leave.
