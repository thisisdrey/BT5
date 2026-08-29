# Q5924: get-egroup via liquidate: compose two individually correct mechanisms into an incorr

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `debt-amount` reach `get-egroup` (mainnet/contracts/market/v0-4-market.clar:460) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it resolves the efficiency group for a mask and is unwrapped with `try!` on every health path, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:460` -> `get-egroup`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `debt-amount`
- Exploit idea: `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `debt-amount` varied, and assert that the value `get-egroup` returns is identical in both runs; a divergence confirms the finding.
