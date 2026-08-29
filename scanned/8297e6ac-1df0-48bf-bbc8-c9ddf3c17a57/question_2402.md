# Q2402: vault-accrue via redeem: compose two individually correct mechanisms into an incorr

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling the vault's available liquidity relative to the redemption, can an unprivileged attacker make `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) compose two individually correct mechanisms into an incorrect result? `vault-accrue` dispatches accrual to one of six vaults by asset id, so the invariant that no position row exists that the position mask does not represent would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `redeem` twice with the vault's available liquidity relative to the redemption varied, and assert that the value `vault-accrue` returns is identical in both runs; a divergence confirms the finding.
