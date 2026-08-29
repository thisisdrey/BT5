# Q0888: interest-rate via repay: satisfy a bound with a value the bound was never designed 

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls whether the repaid asset is in the accrued debt list reach `interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it interpolates the packed curve at the current utilization, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `repay` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz whether the repaid asset is in the accrued debt list across its boundary values through `repay` in simnet and assert `interest-rate` never returns a value that breaks the invariant.
