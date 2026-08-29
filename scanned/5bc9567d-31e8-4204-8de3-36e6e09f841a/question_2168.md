# Q2168: user-safe-mask via liquidate-multi: make two code sites that must agree disagree by an attacke

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls how many entries share one price snapshot (price-feeds is passed as none) reach `user-safe-mask` (mainnet/contracts/market/v0-4-market.clar:428) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it ANDs the user's collateral bits against the enabled bitmap but keeps ALL debt bits unfiltered, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:428` -> `user-safe-mask`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: how many entries share one price snapshot (price-feeds is passed as none)
- Exploit idea: `user-safe-mask` ANDs the user's collateral bits against the enabled bitmap but keeps ALL debt bits unfiltered. Reach it through `liquidate-multi` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with how many entries share one price snapshot (price-feeds is passed as none) varied, and assert that the value `user-safe-mask` returns is identical in both runs; a divergence confirms the finding.
