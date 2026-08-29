# Q3049: send-tokens via borrow: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling the `price-feeds` buffers, drive `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) — which pushes an asset to a caller-chosen recipient principal — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `borrow` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `borrow` with the `price-feeds` buffers, then read `send-tokens` state before and after in the same block and assert the two sides of the invariant are equal.
