# Q5588: get-cached-indexes via accrue: make a health check read a different position than the one

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls the block time at which accrual is first triggered in a block reach `get-cached-indexes` (mainnet/contracts/market/v0-4-market.clar:944) in a state where it make a health check read a different position than the one that will exist? Given that it reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:944` -> `get-cached-indexes`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `get-cached-indexes` reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on. Reach it through `accrue` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `accrue` twice with the block time at which accrual is first triggered in a block varied, and assert that the value `get-cached-indexes` returns is identical in both runs; a divergence confirms the finding.
