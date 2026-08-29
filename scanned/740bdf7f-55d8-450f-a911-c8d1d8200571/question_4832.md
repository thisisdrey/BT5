# Q4832: population via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls call ordering within the block reach `population` (mainnet/contracts/registry/v0-egroup.clar:81) in a state where it convert a rounding direction into a repeatable extraction? Given that it counts set bits to order the bucket search, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:81` -> `population`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: call ordering within the block
- Exploit idea: `population` counts set bits to order the bucket search. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with call ordering within the block varied, and assert that the value `population` returns is identical in both runs; a divergence confirms the finding.
