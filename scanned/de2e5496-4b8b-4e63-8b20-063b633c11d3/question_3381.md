# Q3381: vault-accrue via redeem: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), controlling the vault's available liquidity relative to the redemption, drive `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) — which dispatches accrual to one of six vaults by asset id — to make a health check read a different position than the one that will exist, breaking the invariant that every asset a position holds enters the health evaluation exactly once, and cause permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `vault-accrue` touches, run `redeem` with the vault's available liquidity relative to the redemption, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
