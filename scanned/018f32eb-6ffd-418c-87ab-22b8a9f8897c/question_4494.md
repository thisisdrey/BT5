# Q4494: mod - NFT single-ownership invariant broken by a re-deposit (5)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, exploit the `total_supply > 1` NFT guard around `mt_resolve_deposit_gas` in `contracts/defuse/src/contract/tokens/mod.rs` so the same `Nep171TokenId` is credited twice, or so a legitimate re-deposit after a refund is permanently rejected, breaking the invariant ``total_supplies` for a `Nep171TokenId` is 0 or 1 at every observable point` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/src/contract/tokens/mod.rs](contracts/defuse/src/contract/tokens/mod.rs) - `mt_resolve_deposit_gas` (cross-check `resolve_deposit_internal` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: The guard is checked after `total_supplies.add`; probe the refund/burn path restoring supply and any ordering where supply is not decremented. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: `total_supplies` for a `Nep171TokenId` is 0 or 1 at every observable point
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Deposit, withdraw with a failing transfer, and re-deposit the same NFT; assert supply never exceeds 1 and re-deposit succeeds.
