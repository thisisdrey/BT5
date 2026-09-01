# Q5199: resolver - re-entrancy through an attacker-named callee (11)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, have the contract named in `msg` / `AuthCall::contract_id` / `NotifyOnTransfer` re-enter `execute_intents`, `ft_withdraw` or a `*_resolve_*` path while `mt_resolve_transfer` in `contracts/defuse/src/contract/tokens/nep245/resolver.rs` is mid-settlement, observing or mutating state between the debit and the credit, breaking the invariant `the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/resolver.rs](contracts/defuse/src/contract/tokens/nep245/resolver.rs) - `mt_resolve_transfer`
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `Engine::finalize` has already run when the promise fires; the callee can spend the same balance again or take goods plus refund. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy an `on_auth`/`mt_on_transfer` that calls back into `execute_intents`; assert no balance is spent twice.
