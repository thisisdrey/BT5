# Q2699: mask-update via borrow: satisfy a bound with a value the bound was never designed 

## Question
`mask-update` (mainnet/contracts/market/v0-market-vault.clar:94) sets or clears one bit, clearing only when the row reaches exactly zero. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing `amount`, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:94` -> `mask-update`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `mask-update` sets or clears one bit, clearing only when the row reaches exactly zero. Reach it through `borrow` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `borrow` call, then the attacker-shaped one with `amount`, and assert the attacker's net token balance change is zero or negative.
