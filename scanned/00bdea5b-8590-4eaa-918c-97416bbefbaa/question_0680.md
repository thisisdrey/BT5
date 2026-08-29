# Q0680: iter-lookup-collateral via liquidate: satisfy a bound with a value the bound was never designed 

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `debt-amount` reach `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `debt-amount`
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `liquidate` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `debt-amount` varied, and assert that the value `iter-lookup-collateral` returns is identical in both runs; a divergence confirms the finding.
