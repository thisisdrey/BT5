# Q0980: get-cached-indexes via repay: make an aggregate and its per-item breakdown disagree

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `amount`, including far above the real debt (the capping path) reach `get-cached-indexes` (mainnet/contracts/market/v0-4-market.clar:944) in a state where it make an aggregate and its per-item breakdown disagree? Given that it reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:944` -> `get-cached-indexes`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `get-cached-indexes` reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on. Reach it through `repay` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `amount`, including far above the real debt (the capping path) varied, and assert that the value `get-cached-indexes` returns is identical in both runs; a divergence confirms the finding.
