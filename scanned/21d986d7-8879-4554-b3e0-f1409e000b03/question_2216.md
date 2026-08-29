# Q2216: mask-update via repay: make two code sites that must agree disagree by an attacke

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `on-behalf-of`, naming any third-party principal reach `mask-update` (mainnet/contracts/market/v0-market-vault.clar:94) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it sets or clears one bit, clearing only when the row reaches exactly zero, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:94` -> `mask-update`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `mask-update` sets or clears one bit, clearing only when the row reaches exactly zero. Reach it through `repay` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `on-behalf-of`, naming any third-party principal varied, and assert that the value `mask-update` returns is identical in both runs; a divergence confirms the finding.
