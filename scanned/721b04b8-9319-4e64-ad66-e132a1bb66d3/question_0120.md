# Q0120: resolve via borrow: satisfy a bound with a value the bound was never designed 

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the order of accrual versus price resolution inside the let reach `resolve` (mainnet/contracts/registry/v0-egroup.clar:360) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it selects the efficiency group for a position mask, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:360` -> `resolve`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `resolve` selects the efficiency group for a position mask. Reach it through `borrow` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the order of accrual versus price resolution inside the let across its boundary values through `borrow` in simnet and assert `resolve` never returns a value that breaks the invariant.
