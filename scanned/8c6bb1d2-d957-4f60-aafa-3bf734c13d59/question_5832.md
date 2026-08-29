# Q5832: write-feed via call-ststx-ratio: make a health check read a different position than the one

## Question
Does `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) let an unprivileged attacker who controls the block and transaction position at which the external ratio is fetched reach `write-feed` (mainnet/contracts/market/v0-4-market.clar:129) in a state where it make a health check read a different position than the one that will exist? Given that it applies one Pyth price-feed update and folds its status, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:129` -> `write-feed`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: the block and transaction position at which the external ratio is fetched
- Exploit idea: `write-feed` applies one Pyth price-feed update and folds its status. Reach it through `call-ststx-ratio` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the block and transaction position at which the external ratio is fetched across its boundary values through `call-ststx-ratio` in simnet and assert `write-feed` never returns a value that breaks the invariant.
