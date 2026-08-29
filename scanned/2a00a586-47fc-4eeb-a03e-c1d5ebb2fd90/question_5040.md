# Q5040: linear-interpolate via borrow: make a health check read a different position than the one

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the future mask produced by the new debt bit reach `linear-interpolate` (mainnet/contracts/vault/v0-vault-stx.clar:221) in a state where it make a health check read a different position than the one that will exist? Given that it interpolates between two points, dividing by `(- x2 x1)`, the invariant that conversions never round in the user's favour in either direction breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:221` -> `linear-interpolate`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the future mask produced by the new debt bit
- Exploit idea: `linear-interpolate` interpolates between two points, dividing by `(- x2 x1)`. Reach it through `borrow` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the future mask produced by the new debt bit across its boundary values through `borrow` in simnet and assert `linear-interpolate` never returns a value that breaks the invariant.
