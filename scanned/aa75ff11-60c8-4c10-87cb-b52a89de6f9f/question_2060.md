# Q2060: receive-tokens via liquidate: make two code sites that must agree disagree by an attacke

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `collateral-receiver` reach `receive-tokens` (mainnet/contracts/market/v0-market-vault.clar:256) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it pulls an asset from a named account, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:256` -> `receive-tokens`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `receive-tokens` pulls an asset from a named account. Reach it through `liquidate` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `collateral-receiver` varied, and assert that the value `receive-tokens` returns is identical in both runs; a divergence confirms the finding.
