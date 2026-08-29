# Q1386: remove-user-collateral via repay: make a health check read a different position than the one

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling the `ft` trait principal, can an unprivileged attacker make `remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) make a health check read a different position than the one that will exist? `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero, so the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `repay` and make a health check read a different position than the one that will exist.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `repay` in simnet and assert `remove-user-collateral` never returns a value that breaks the invariant.
