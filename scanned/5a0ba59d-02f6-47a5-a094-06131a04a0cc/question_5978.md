# Q5978: accrue-collateral-asset via repay: make two code sites that must agree disagree by an attacke

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling whether the repaid asset is in the accrued debt list, can an unprivileged attacker make `accrue-collateral-asset` (mainnet/contracts/market/v0-4-market.clar:273) make two code sites that must agree disagree by an attacker-chosen amount? `accrue-collateral-asset` maps a ztoken id to a vault id through a chain of `is-eq` tests that falls through to the u100 sentinel, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:273` -> `accrue-collateral-asset`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `accrue-collateral-asset` maps a ztoken id to a vault id through a chain of `is-eq` tests that falls through to the u100 sentinel. Reach it through `repay` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with whether the repaid asset is in the accrued debt list varied, and assert that the value `accrue-collateral-asset` returns is identical in both runs; a divergence confirms the finding.
