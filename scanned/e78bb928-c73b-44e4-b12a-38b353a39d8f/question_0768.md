# Q0768: find-asset via liquidate-redeem: satisfy a bound with a value the bound was never designed 

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the borrower targeted reach `find-asset` (mainnet/contracts/market/v0-4-market.clar:584) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:584` -> `find-asset`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Reach it through `liquidate-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the borrower targeted across its boundary values through `liquidate-redeem` in simnet and assert `find-asset` never returns a value that breaks the invariant.
