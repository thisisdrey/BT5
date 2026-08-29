# Q0964: unpack-u16 via accrue: satisfy a bound with a value the bound was never designed 

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls the block time at which accrual is first triggered in a block reach `unpack-u16` (mainnet/contracts/vault/v0-vault-stx.clar:259) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it unpacks eight u16 curve fields from one packed word, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:259` -> `unpack-u16`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `unpack-u16` unpacks eight u16 curve fields from one packed word. Reach it through `accrue` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `accrue` with the block time at which accrual is first triggered in a block, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
