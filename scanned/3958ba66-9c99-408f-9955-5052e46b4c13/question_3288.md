# Q3288: create via collateral-remove: reach a state the guard immediately upstream of it never c

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `ft` trait principal reach `create` (mainnet/contracts/market/v0-market-vault.clar:150) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it binds a principal to a fresh numeric id, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:150` -> `create`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `create` binds a principal to a fresh numeric id. Reach it through `collateral-remove` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `collateral-remove` in simnet and assert `create` never returns a value that breaks the invariant.
