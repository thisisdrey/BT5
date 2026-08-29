# Q2160: interpolate-rate via redeem: make two code sites that must agree disagree by an attacke

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls `min-out` reach `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it interpolates between packed u16 curve points, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `min-out` across its boundary values through `redeem` in simnet and assert `interpolate-rate` never returns a value that breaks the invariant.
