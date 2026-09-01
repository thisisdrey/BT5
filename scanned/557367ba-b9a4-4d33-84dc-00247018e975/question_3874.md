# Q3874: deposit - NFT single-ownership invariant broken by a re-deposit (2)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, exploit the `total_supply > 1` NFT guard around `nft_on_transfer` in `contracts/defuse/src/contract/tokens/nep171/deposit.rs` so the same `Nep171TokenId` is credited twice, or so a legitimate re-deposit after a refund is permanently rejected, breaking the invariant ``total_supplies` for a `Nep171TokenId` is 0 or 1 at every observable point` and leading to unauthorized minting / balance inflation: a balance is credited with no matching asset received?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/deposit.rs](contracts/defuse/src/contract/tokens/nep171/deposit.rs) - `nft_on_transfer` (cross-check `nft_resolve_deposit` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: The guard is checked after `total_supplies.add`; probe the refund/burn path restoring supply and any ordering where supply is not decremented. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: `total_supplies` for a `Nep171TokenId` is 0 or 1 at every observable point
- Expected Immunefi impact: Critical - Unauthorized minting / balance inflation: a balance is credited with no matching asset received
- Fast validation: Deposit, withdraw with a failing transfer, and re-deposit the same NFT; assert supply never exceeds 1 and re-deposit succeeds.
