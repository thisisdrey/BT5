# Q4147: enumeration - NFT single-ownership invariant broken by a re-deposit (16)

## Question
Given the token contract is one the attacker deployed and can fail on demand, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, exploit the `total_supply > 1` NFT guard around `mt_tokens_for_owner` in `contracts/defuse/src/contract/tokens/nep245/enumeration.rs` so the same `Nep171TokenId` is credited twice, or so a legitimate re-deposit after a refund is permanently rejected, breaking the invariant ``total_supplies` for a `Nep171TokenId` is 0 or 1 at every observable point` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/enumeration.rs](contracts/defuse/src/contract/tokens/nep245/enumeration.rs) - `mt_tokens_for_owner` (cross-check `mt_tokens` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: The guard is checked after `total_supplies.add`; probe the refund/burn path restoring supply and any ordering where supply is not decremented. Set-up: the token contract is one the attacker deployed and can fail on demand.
- Invariant to test: `total_supplies` for a `Nep171TokenId` is 0 or 1 at every observable point
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Deposit, withdraw with a failing transfer, and re-deposit the same NFT; assert supply never exceeds 1 and re-deposit succeeds.
