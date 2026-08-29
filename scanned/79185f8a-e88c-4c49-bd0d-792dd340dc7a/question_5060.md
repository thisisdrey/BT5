# Q5060: find via liquidate: make a health check read a different position than the one

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `borrower`, any third-party principal reach `find` (mainnet/contracts/registry/v0-assets.clar:135) in a state where it make a health check read a different position than the one that will exist? Given that it resolves an asset record from a principal through the `reverse` map, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:135` -> `find`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `find` resolves an asset record from a principal through the `reverse` map. Reach it through `liquidate` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `borrower`, any third-party principal varied, and assert that the value `find` returns is identical in both runs; a divergence confirms the finding.
