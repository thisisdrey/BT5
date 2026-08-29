# Q3900: calc-multiplier-delta via borrow: reach a state the guard immediately upstream of it never c

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the `ft` trait principal reach `calc-multiplier-delta` (mainnet/contracts/vault/v0-vault-stx.clar:170) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it compounds a rate over `time-delta` with a caller-independent rounding flag, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:170` -> `calc-multiplier-delta`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `calc-multiplier-delta` compounds a rate over `time-delta` with a caller-independent rounding flag. Reach it through `borrow` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `borrow` in simnet and assert `calc-multiplier-delta` never returns a value that breaks the invariant.
