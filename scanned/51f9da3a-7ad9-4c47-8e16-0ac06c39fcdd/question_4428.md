# Q4428: uint-to-list-u64 via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls whether the position has any enabled debt row (the has-debt branch) reach `uint-to-list-u64` (mainnet/contracts/registry/v0-assets.clar:80) in a state where it convert a rounding direction into a repeatable extraction? Given that it expands a bitmap into a 64-element list, the invariant that no position row exists that the position mask does not represent breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:80` -> `uint-to-list-u64`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `uint-to-list-u64` expands a bitmap into a 64-element list. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz whether the position has any enabled debt row (the has-debt branch) across its boundary values through `collateral-remove` in simnet and assert `uint-to-list-u64` never returns a value that breaks the invariant.
