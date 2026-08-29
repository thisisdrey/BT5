# Q2802: filter-u128 via borrow: compose two individually correct mechanisms into an incorr

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `receiver`, including a contract principal, can an unprivileged attacker make `filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) compose two individually correct mechanisms into an incorrect result? `filter-u128` filters a 128-entry bucket list, so the invariant that no position row exists that the position mask does not represent would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `borrow` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `borrow` in simnet and assert `filter-u128` never returns a value that breaks the invariant.
