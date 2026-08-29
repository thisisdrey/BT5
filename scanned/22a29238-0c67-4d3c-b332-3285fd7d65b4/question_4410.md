# Q4410: total-debt via collateral-remove-redeem: satisfy a bound with a value the bound was never designed 

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling the zToken/underlying id mapping reached (the u100 sentinel branch), can an unprivileged attacker make `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) satisfy a bound with a value the bound was never designed to admit? `total-debt` computes cumulative debt from `principal-scaled` and `index`, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: the zToken/underlying id mapping reached (the u100 sentinel branch)
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `collateral-remove-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the zToken/underlying id mapping reached (the u100 sentinel branch) across its boundary values through `collateral-remove-redeem` in simnet and assert `total-debt` never returns a value that breaks the invariant.
