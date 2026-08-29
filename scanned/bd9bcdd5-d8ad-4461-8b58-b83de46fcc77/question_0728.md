# Q0728: remove-user-collateral via transfer: satisfy a bound with a value the bound was never designed 

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls `amount` reach `remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it asserts sufficiency then `map-delete`s only on an exact zero, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `transfer` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `transfer` twice with `amount` varied, and assert that the value `remove-user-collateral` returns is identical in both runs; a divergence confirms the finding.
