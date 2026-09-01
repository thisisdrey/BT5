# Q5257: deposit - deposit credited before the source transfer is final (13)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, make `nft_resolve_deposit` in `contracts/defuse/src/contract/tokens/nep171/deposit.rs` credit a balance from a token contract the attacker controls, then reverse the underlying transfer, so the Verifier holds a balance backed by nothing, breaking the invariant `every credited balance == an asset the Verifier can actually redeem from the issuing contract` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/deposit.rs](contracts/defuse/src/contract/tokens/nep171/deposit.rs) - `nft_resolve_deposit` (cross-check `nft_on_transfer` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `ft_on_transfer` credits on the strength of `env::predecessor_account_id()` alone; a malicious token can credit and then refuse to hold the assets. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: every credited balance == an asset the Verifier can actually redeem from the issuing contract
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Deploy a token that credits then self-reverses; assert the Verifier's accounting detects it.
