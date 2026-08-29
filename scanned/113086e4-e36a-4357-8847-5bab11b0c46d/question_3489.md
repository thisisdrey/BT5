# Q3489: calc-index-next via call-ststx-ratio: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015), controlling whether the ratio is fetched before or after other state changes in the block, drive `calc-index-next` (mainnet/contracts/vault/v0-vault-stx.clar:183) — which applies a multiplier to the current index — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:183` -> `calc-index-next`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: whether the ratio is fetched before or after other state changes in the block
- Exploit idea: `calc-index-next` applies a multiplier to the current index. Reach it through `call-ststx-ratio` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `calc-index-next` touches, run `call-ststx-ratio` with whether the ratio is fetched before or after other state changes in the block, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
