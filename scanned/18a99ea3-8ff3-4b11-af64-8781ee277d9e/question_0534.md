# Q0534: add-user-scaled-debt via borrow: convert a rounding direction into a repeatable extraction

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling the order of accrual versus price resolution inside the let, can an unprivileged attacker make `add-user-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:237) convert a rounding direction into a repeatable extraction? `add-user-scaled-debt` adds to the scaled debt row with a graceful u0 default, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:237` -> `add-user-scaled-debt`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `add-user-scaled-debt` adds to the scaled debt row with a graceful u0 default. Reach it through `borrow` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the order of accrual versus price resolution inside the let across its boundary values through `borrow` in simnet and assert `add-user-scaled-debt` never returns a value that breaks the invariant.
