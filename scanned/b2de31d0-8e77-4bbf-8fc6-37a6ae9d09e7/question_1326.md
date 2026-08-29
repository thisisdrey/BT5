# Q1326: vault-accrue via call-ststx-ratio: make a health check read a different position than the one

## Question
Entering through `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) while controlling the block and transaction position at which the external ratio is fetched, can an unprivileged attacker make `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) make a health check read a different position than the one that will exist? `vault-accrue` dispatches accrual to one of six vaults by asset id, so the invariant that only the acting principal's own position is mutated would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: the block and transaction position at which the external ratio is fetched
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `call-ststx-ratio` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the block and transaction position at which the external ratio is fetched across its boundary values through `call-ststx-ratio` in simnet and assert `vault-accrue` never returns a value that breaks the invariant.
