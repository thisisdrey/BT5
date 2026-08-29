# Q3698: subset via collateral-remove: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling the `price-feeds` buffers, can an unprivileged attacker make `subset` (mainnet/contracts/market/v0-market-vault.clar:100) turn an accounting residue into a permanently unclosable position? `subset` tests bitmask containment, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:100` -> `subset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `subset` tests bitmask containment. Reach it through `collateral-remove` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with the `price-feeds` buffers varied, and assert that the value `subset` returns is identical in both runs; a divergence confirms the finding.
