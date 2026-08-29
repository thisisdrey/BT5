# Q4608: uint-to-list-u64 via borrow: convert a rounding direction into a repeatable extraction

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the `price-feeds` buffers reach `uint-to-list-u64` (mainnet/contracts/registry/v0-assets.clar:80) in a state where it convert a rounding direction into a repeatable extraction? Given that it expands a bitmap into a 64-element list, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:80` -> `uint-to-list-u64`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `uint-to-list-u64` expands a bitmap into a 64-element list. Reach it through `borrow` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the `price-feeds` buffers across its boundary values through `borrow` in simnet and assert `uint-to-list-u64` never returns a value that breaks the invariant.
