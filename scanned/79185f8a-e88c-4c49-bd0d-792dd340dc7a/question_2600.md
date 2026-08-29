# Q2600: insert via repay: make two code sites that must agree disagree by an attacke

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `on-behalf-of`, naming any third-party principal reach `insert` (mainnet/contracts/market/v0-market-vault.clar:159) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it rewrites the whole registry entry for a user id, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:159` -> `insert`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `insert` rewrites the whole registry entry for a user id. Reach it through `repay` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `on-behalf-of`, naming any third-party principal varied, and assert that the value `insert` returns is identical in both runs; a divergence confirms the finding.
