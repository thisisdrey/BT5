# Q3384: active via borrow: reach a state the guard immediately upstream of it never c

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the `ft` trait principal reach `active` (mainnet/contracts/registry/v0-egroup.clar:238) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it lists candidate bucket masks at or above a population, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:238` -> `active`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `active` lists candidate bucket masks at or above a population. Reach it through `borrow` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `borrow` in simnet and assert `active` never returns a value that breaks the invariant.
