# Q5708: lookup via liquidate: make a health check read a different position than the one

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `borrower`, any third-party principal reach `lookup` (mainnet/contracts/registry/v0-assets.clar:139) in a state where it make a health check read a different position than the one that will exist? Given that it returns the registry record, including the `decimals` captured once at registration, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:139` -> `lookup`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `lookup` returns the registry record, including the `decimals` captured once at registration. Reach it through `liquidate` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `borrower`, any third-party principal varied, and assert that the value `lookup` returns is identical in both runs; a divergence confirms the finding.
