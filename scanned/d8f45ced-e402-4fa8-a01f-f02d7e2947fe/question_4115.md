# Q4115: mod - deposit credited before the source transfer is final (3)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, make `MT_RESOLVE_DEPOSIT_PER_TOKEN_GAS` in `contracts/defuse/src/contract/tokens/mod.rs` credit a balance from a token contract the attacker controls, then reverse the underlying transfer, so the Verifier holds a balance backed by nothing, breaking the invariant `every credited balance == an asset the Verifier can actually redeem from the issuing contract` and leading to protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier?

## Target
- File/function: [contracts/defuse/src/contract/tokens/mod.rs](contracts/defuse/src/contract/tokens/mod.rs) - `MT_RESOLVE_DEPOSIT_PER_TOKEN_GAS` (cross-check `withdraw` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: `ft_on_transfer` credits on the strength of `env::predecessor_account_id()` alone; a malicious token can credit and then refuse to hold the assets. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: every credited balance == an asset the Verifier can actually redeem from the issuing contract
- Expected Immunefi impact: Critical - Protocol insolvency: sum of `token_balances` owed exceeds assets actually custodied by the Verifier
- Fast validation: Deploy a token that credits then self-reverses; assert the Verifier's accounting detects it.
