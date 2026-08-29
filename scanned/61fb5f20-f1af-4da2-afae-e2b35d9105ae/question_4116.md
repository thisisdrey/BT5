# Q4116: normalize via collateral-remove-redeem: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls remaining zToken collateral whose price moves with the redeem reach `normalize` (mainnet/contracts/market/v0-4-market.clar:576) in a state where it convert a rounding direction into a repeatable extraction? Given that it divides by `(pow u10 decimals)` only AFTER multiplying amount by price, making the protocol's USD unit a whole dollar, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:576` -> `normalize`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: remaining zToken collateral whose price moves with the redeem
- Exploit idea: `normalize` divides by `(pow u10 decimals)` only AFTER multiplying amount by price, making the protocol's USD unit a whole dollar. Reach it through `collateral-remove-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz remaining zToken collateral whose price moves with the redeem across its boundary values through `collateral-remove-redeem` in simnet and assert `normalize` never returns a value that breaks the invariant.
