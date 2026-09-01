# Q5748: deposit - re-entrancy through an attacker-named callee (29)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, have the contract named in `msg` / `AuthCall::contract_id` / `NotifyOnTransfer` re-enter `execute_intents`, `ft_withdraw` or a `*_resolve_*` path while `mt_resolve_deposit` in `contracts/defuse/src/contract/tokens/nep245/deposit.rs` is mid-settlement, observing or mutating state between the debit and the credit, breaking the invariant `the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/deposit.rs](contracts/defuse/src/contract/tokens/nep245/deposit.rs) - `mt_resolve_deposit` (cross-check `mt_on_transfer` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: `Engine::finalize` has already run when the promise fires; the callee can spend the same balance again or take goods plus refund. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy an `on_auth`/`mt_on_transfer` that calls back into `execute_intents`; assert no balance is spent twice.
