# Q5447: calc-index-next via call-ststx-ratio: reach a state the guard immediately upstream of it never c

## Question
`calc-index-next` (mainnet/contracts/vault/v0-vault-stx.clar:183) applies a multiplier to the current index. Can an unprivileged caller of `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015), by choosing whether the ratio is fetched before or after other state changes in the block, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:183` -> `calc-index-next`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: whether the ratio is fetched before or after other state changes in the block
- Exploit idea: `calc-index-next` applies a multiplier to the current index. Reach it through `call-ststx-ratio` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `call-ststx-ratio` call, then the attacker-shaped one with whether the ratio is fetched before or after other state changes in the block, and assert the attacker's net token balance change is zero or negative.
