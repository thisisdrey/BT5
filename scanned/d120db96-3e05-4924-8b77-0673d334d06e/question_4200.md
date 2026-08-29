# Q4200: get-notional-evaluation via collateral-remove-redeem: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls the zToken/underlying id mapping reached (the u100 sentinel branch) reach `get-notional-evaluation` (mainnet/contracts/market/v0-4-market.clar:514) in a state where it convert a rounding direction into a repeatable extraction? Given that it folds over the ENABLED asset list, so a position row whose asset is absent from that list contributes nothing to either total, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:514` -> `get-notional-evaluation`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: the zToken/underlying id mapping reached (the u100 sentinel branch)
- Exploit idea: `get-notional-evaluation` folds over the ENABLED asset list, so a position row whose asset is absent from that list contributes nothing to either total. Reach it through `collateral-remove-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the zToken/underlying id mapping reached (the u100 sentinel branch) across its boundary values through `collateral-remove-redeem` in simnet and assert `get-notional-evaluation` never returns a value that breaks the invariant.
