# Q5528: total-debt via transfer: make a health check read a different position than the one

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls the destination principal, including the market, the market-vault or the treasury reach `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) in a state where it make a health check read a different position than the one that will exist? Given that it computes cumulative debt from `principal-scaled` and `index`, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the destination principal, including the market, the market-vault or the treasury
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `transfer` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with the destination principal, including the market, the market-vault or the treasury varied, and assert that the value `total-debt` returns is identical in both runs; a divergence confirms the finding.
