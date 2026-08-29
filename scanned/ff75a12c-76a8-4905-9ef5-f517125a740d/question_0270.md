# Q0270: vault-system-borrow via borrow: convert a rounding direction into a repeatable extraction

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling the order of accrual versus price resolution inside the let, can an unprivileged attacker make `vault-system-borrow` (mainnet/contracts/market/v0-4-market.clar:198) convert a rounding direction into a repeatable extraction? `vault-system-borrow` routes a borrow to one of six vaults by asset id, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:198` -> `vault-system-borrow`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `vault-system-borrow` routes a borrow to one of six vaults by asset id. Reach it through `borrow` and convert a rounding direction into a repeatable extraction.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the order of accrual versus price resolution inside the let across its boundary values through `borrow` in simnet and assert `vault-system-borrow` never returns a value that breaks the invariant.
