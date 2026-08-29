# Q0872: calc-index-next via collateral-remove: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls `receiver`, including a contract principal reach `calc-index-next` (mainnet/contracts/vault/v0-vault-stx.clar:183) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it applies a multiplier to the current index, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:183` -> `calc-index-next`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `calc-index-next` applies a multiplier to the current index. Reach it through `collateral-remove` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with `receiver`, including a contract principal varied, and assert that the value `calc-index-next` returns is identical in both runs; a divergence confirms the finding.
