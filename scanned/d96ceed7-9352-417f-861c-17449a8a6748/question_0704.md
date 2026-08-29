# Q0704: call-liquidate via liquidate-redeem: satisfy a bound with a value the bound was never designed 

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the borrower targeted reach `call-liquidate` (mainnet/contracts/market/v0-4-market.clar:907) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it invokes `liquidate` with `none` for price-feeds, so a whole batch shares one snapshot, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:907` -> `call-liquidate`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `call-liquidate` invokes `liquidate` with `none` for price-feeds, so a whole batch shares one snapshot. Reach it through `liquidate-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the borrower targeted varied, and assert that the value `call-liquidate` returns is identical in both runs; a divergence confirms the finding.
