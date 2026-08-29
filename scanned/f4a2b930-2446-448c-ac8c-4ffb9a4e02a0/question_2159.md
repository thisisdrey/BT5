# Q2159: debt-add-scaled via borrow: satisfy a bound with a value the bound was never designed 

## Question
`debt-add-scaled` (mainnet/contracts/market/v0-market-vault.clar:442) stamps `last-borrow-block` from `stacks-block-height` onto the named ACCOUNT, not the caller. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing `amount`, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:442` -> `debt-add-scaled`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `debt-add-scaled` stamps `last-borrow-block` from `stacks-block-height` onto the named ACCOUNT, not the caller. Reach it through `borrow` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `borrow` call, then the attacker-shaped one with `amount`, and assert the attacker's net token balance change is zero or negative.
