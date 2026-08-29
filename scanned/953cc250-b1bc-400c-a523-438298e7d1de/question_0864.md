# Q0864: remove-user-collateral via liquidate-multi: satisfy a bound with a value the bound was never designed 

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls the trait principals supplied per entry reach `remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it asserts sufficiency then `map-delete`s only on an exact zero, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the trait principals supplied per entry
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `liquidate-multi` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the trait principals supplied per entry across its boundary values through `liquidate-multi` in simnet and assert `remove-user-collateral` never returns a value that breaks the invariant.
