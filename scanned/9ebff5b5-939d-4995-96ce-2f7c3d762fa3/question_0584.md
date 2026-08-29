# Q0584: accrue-user-debts via redeem: satisfy a bound with a value the bound was never designed 

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls the vault's available liquidity relative to the redemption reach `accrue-user-debts` (mainnet/contracts/market/v0-4-market.clar:259) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it folds accrual over the position's debt list only, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:259` -> `accrue-user-debts`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `accrue-user-debts` folds accrual over the position's debt list only. Reach it through `redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `redeem` twice with the vault's available liquidity relative to the redemption varied, and assert that the value `accrue-user-debts` returns is identical in both runs; a divergence confirms the finding.
