# Q5492: find-superset via collateral-add: make a health check read a different position than the one

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the `ft` trait principal reach `find-superset` (mainnet/contracts/registry/v0-egroup.clar:262) in a state where it make a health check read a different position than the one that will exist? Given that it returns the FIRST mask that is a superset, walking buckets in population order rather than finding the tightest, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:262` -> `find-superset`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `find-superset` returns the FIRST mask that is a superset, walking buckets in population order rather than finding the tightest. Reach it through `collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with the `ft` trait principal varied, and assert that the value `find-superset` returns is identical in both runs; a divergence confirms the finding.
