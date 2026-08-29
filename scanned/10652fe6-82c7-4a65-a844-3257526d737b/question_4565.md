# Q4565: send-tokens via collateral-add: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling the three `price-feeds` buffers and their order, drive `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) — which pushes an asset to a caller-chosen recipient principal — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that only the acting principal's own position is mutated, and cause protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the three `price-feeds` buffers and their order
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `collateral-add` call, then the attacker-shaped one with the three `price-feeds` buffers and their order, and assert the attacker's net token balance change is zero or negative.
