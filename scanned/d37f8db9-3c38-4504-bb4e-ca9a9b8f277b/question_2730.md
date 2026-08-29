# Q2730: remove-user-collateral via liquidate: compose two individually correct mechanisms into an incorr

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `debt-amount`, can an unprivileged attacker make `remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) compose two individually correct mechanisms into an incorrect result? `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero, so the invariant that no position row exists that the position mask does not represent would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `debt-amount`
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `debt-amount` across its boundary values through `liquidate` in simnet and assert `remove-user-collateral` never returns a value that breaks the invariant.
