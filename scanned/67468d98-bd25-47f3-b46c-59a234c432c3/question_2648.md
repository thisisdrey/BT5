# Q2648: interpolate-rate via call-ststx-ratio: make two code sites that must agree disagree by an attacke

## Question
Does `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) let an unprivileged attacker who controls whether the ratio is fetched before or after other state changes in the block reach `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it interpolates between packed u16 curve points, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: whether the ratio is fetched before or after other state changes in the block
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `call-ststx-ratio` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `call-ststx-ratio` twice with whether the ratio is fetched before or after other state changes in the block varied, and assert that the value `interpolate-rate` returns is identical in both runs; a divergence confirms the finding.
