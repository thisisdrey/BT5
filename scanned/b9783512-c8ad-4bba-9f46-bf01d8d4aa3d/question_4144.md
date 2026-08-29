# Q4144: debt-remove-scaled via transfer: convert a rounding direction into a repeatable extraction

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls `amount` reach `debt-remove-scaled` (mainnet/contracts/market/v0-market-vault.clar:473) in a state where it convert a rounding direction into a repeatable extraction? Given that it clears the debt bit only when the remaining scaled debt is exactly zero, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:473` -> `debt-remove-scaled`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `debt-remove-scaled` clears the debt bit only when the remaining scaled debt is exactly zero. Reach it through `transfer` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `transfer` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
