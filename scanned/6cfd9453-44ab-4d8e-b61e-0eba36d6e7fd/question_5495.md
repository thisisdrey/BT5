# Q5495: auth_call - re-entrancy through an attacker-named callee (14)

## Question
Given the named receiver account does not exist on chain, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, have the contract named in `msg` / `AuthCall::contract_id` / `NotifyOnTransfer` re-enter `execute_intents`, `ft_withdraw` or a `*_resolve_*` path while `do_auth_call` in `contracts/defuse/src/contract/intents/auth_call.rs` is mid-settlement, observing or mutating state between the debit and the credit, breaking the invariant `the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/intents/auth_call.rs](contracts/defuse/src/contract/intents/auth_call.rs) - `do_auth_call` (cross-check `DO_AUTH_CALL_MIN_GAS` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: `Engine::finalize` has already run when the promise fires; the callee can spend the same balance again or take goods plus refund. Set-up: the named receiver account does not exist on chain.
- Invariant to test: the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy an `on_auth`/`mt_on_transfer` that calls back into `execute_intents`; assert no balance is spent twice.
