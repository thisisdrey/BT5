# Q5048: resolve-dia via collateral-remove: make a health check read a different position than the one

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls `amount` relative to the current collateral row (the removing-all branch) reach `resolve-dia` (mainnet/contracts/market/v0-4-market.clar:326) in a state where it make a health check read a different position than the one that will exist? Given that it derives a (string-ascii 32) key from a (buff 32) ident, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:326` -> `resolve-dia`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `amount` relative to the current collateral row (the removing-all branch)
- Exploit idea: `resolve-dia` derives a (string-ascii 32) key from a (buff 32) ident. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with `amount` relative to the current collateral row (the removing-all branch) varied, and assert that the value `resolve-dia` returns is identical in both runs; a divergence confirms the finding.
