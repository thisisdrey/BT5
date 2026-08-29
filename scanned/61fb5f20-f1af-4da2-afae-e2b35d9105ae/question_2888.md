# Q2888: create via collateral-remove-redeem: make two code sites that must agree disagree by an attacke

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls remaining zToken collateral whose price moves with the redeem reach `create` (mainnet/contracts/market/v0-market-vault.clar:150) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it binds a principal to a fresh numeric id, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:150` -> `create`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: remaining zToken collateral whose price moves with the redeem
- Exploit idea: `create` binds a principal to a fresh numeric id. Reach it through `collateral-remove-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with remaining zToken collateral whose price moves with the redeem varied, and assert that the value `create` returns is identical in both runs; a divergence confirms the finding.
