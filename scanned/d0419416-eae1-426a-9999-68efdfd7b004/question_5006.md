# Q5006: deposit - deposit credited before the source transfer is final (11)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, make `ft_on_transfer` in `contracts/defuse/src/contract/tokens/nep141/deposit.rs` credit a balance from a token contract the attacker controls, then reverse the underlying transfer, so the Verifier holds a balance backed by nothing, breaking the invariant `every credited balance == an asset the Verifier can actually redeem from the issuing contract` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep141/deposit.rs](contracts/defuse/src/contract/tokens/nep141/deposit.rs) - `ft_on_transfer` (cross-check `ft_resolve_deposit` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: `ft_on_transfer` credits on the strength of `env::predecessor_account_id()` alone; a malicious token can credit and then refuse to hold the assets. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: every credited balance == an asset the Verifier can actually redeem from the issuing contract
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Deploy a token that credits then self-reverses; assert the Verifier's accounting detects it.
