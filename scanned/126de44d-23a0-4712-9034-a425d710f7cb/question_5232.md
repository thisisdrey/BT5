# Q5232: subset via liquidate: make a health check read a different position than the one

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls which collateral and debt asset pair is targeted reach `subset` (mainnet/contracts/market/v0-market-vault.clar:100) in a state where it make a health check read a different position than the one that will exist? Given that it tests bitmask containment, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:100` -> `subset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `subset` tests bitmask containment. Reach it through `liquidate` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz which collateral and debt asset pair is targeted across its boundary values through `liquidate` in simnet and assert `subset` never returns a value that breaks the invariant.
