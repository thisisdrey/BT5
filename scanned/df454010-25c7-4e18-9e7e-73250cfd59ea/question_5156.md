# Q5156: get-account-scaled-debt via liquidate: make a health check read a different position than the one

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `borrower`, any third-party principal reach `get-account-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:307) in a state where it make a health check read a different position than the one that will exist? Given that it reads one scaled debt row, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:307` -> `get-account-scaled-debt`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `get-account-scaled-debt` reads one scaled debt row. Reach it through `liquidate` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `borrower`, any third-party principal varied, and assert that the value `get-account-scaled-debt` returns is identical in both runs; a divergence confirms the finding.
