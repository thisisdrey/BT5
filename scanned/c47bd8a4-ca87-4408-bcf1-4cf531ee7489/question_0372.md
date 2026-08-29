# Q0372: debt-add-scaled via collateral-remove-redeem: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls `min-underlying` reach `debt-add-scaled` (mainnet/contracts/market/v0-market-vault.clar:442) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it stamps `last-borrow-block` from `stacks-block-height` onto the named ACCOUNT, not the caller, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:442` -> `debt-add-scaled`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `min-underlying`
- Exploit idea: `debt-add-scaled` stamps `last-borrow-block` from `stacks-block-height` onto the named ACCOUNT, not the caller. Reach it through `collateral-remove-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-underlying` across its boundary values through `collateral-remove-redeem` in simnet and assert `debt-add-scaled` never returns a value that breaks the invariant.
