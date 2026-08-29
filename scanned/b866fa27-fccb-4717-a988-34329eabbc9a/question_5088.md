# Q5088: convert-to-scaled-debt via supply-collateral-add: make a health check read a different position than the one

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the `ft` trait principal deciding which vault is routed to reach `convert-to-scaled-debt` (mainnet/contracts/market/v0-4-market.clar:648) in a state where it make a health check read a different position than the one that will exist? Given that it scales a token amount by the cached borrow index, rounding up on the borrow path, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:648` -> `convert-to-scaled-debt`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `convert-to-scaled-debt` scales a token amount by the cached borrow index, rounding up on the borrow path. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `ft` trait principal deciding which vault is routed to across its boundary values through `supply-collateral-add` in simnet and assert `convert-to-scaled-debt` never returns a value that breaks the invariant.
