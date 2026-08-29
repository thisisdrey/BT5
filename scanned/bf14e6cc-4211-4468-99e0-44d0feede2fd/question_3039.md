# Q3039: receive-tokens via liquidate: make an aggregate and its per-item breakdown disagree

## Question
`receive-tokens` (mainnet/contracts/market/v0-market-vault.clar:256) pulls an asset from a named account. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing the `price-feeds` buffers and their ordering, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:256` -> `receive-tokens`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers and their ordering
- Exploit idea: `receive-tokens` pulls an asset from a named account. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `receive-tokens` touches, run `liquidate` with the `price-feeds` buffers and their ordering, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
