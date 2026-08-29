# Q2007: calc-utilization via call-ststx-ratio: satisfy a bound with a value the bound was never designed 

## Question
`calc-utilization` (mainnet/contracts/vault/v0-vault-stx.clar:164) divides debt by available liquidity, which can exceed BPS when debt outruns assets. Can an unprivileged caller of `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015), by choosing the block and transaction position at which the external ratio is fetched, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:164` -> `calc-utilization`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: the block and transaction position at which the external ratio is fetched
- Exploit idea: `calc-utilization` divides debt by available liquidity, which can exceed BPS when debt outruns assets. Reach it through `call-ststx-ratio` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `calc-utilization` touches, run `call-ststx-ratio` with the block and transaction position at which the external ratio is fetched, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
