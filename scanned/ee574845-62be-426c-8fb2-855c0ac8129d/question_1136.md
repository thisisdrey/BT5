# Q1136: nep171 - re-entrancy through an attacker-named callee

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, have the contract named in `msg` / `AuthCall::contract_id` / `NotifyOnTransfer` re-enter `execute_intents`, `ft_withdraw` or a `*_resolve_*` path while `nft_withdraw` in `contracts/defuse/src/tokens/nep171.rs` is mid-settlement, observing or mutating state between the debit and the credit, breaking the invariant `the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/tokens/nep171.rs](contracts/defuse/src/tokens/nep171.rs) - `nft_withdraw` (cross-check `NonFungibleTokenForceWithdrawer` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `Engine::finalize` has already run when the promise fires; the callee can spend the same balance again or take goods plus refund. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy an `on_auth`/`mt_on_transfer` that calls back into `execute_intents`; assert no balance is spent twice.
