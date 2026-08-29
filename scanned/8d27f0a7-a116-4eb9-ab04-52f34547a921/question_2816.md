# Q2816: mask-pos via repay: make two code sites that must agree disagree by an attacke

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `on-behalf-of`, naming any third-party principal reach `mask-pos` (mainnet/contracts/market/v0-market-vault.clar:91) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:91` -> `mask-pos`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `mask-pos` maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET. Reach it through `repay` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `on-behalf-of`, naming any third-party principal varied, and assert that the value `mask-pos` returns is identical in both runs; a divergence confirms the finding.
