# Q4500: accrue-user-debts via redeem: convert a rounding direction into a repeatable extraction

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls the vault's available liquidity relative to the redemption reach `accrue-user-debts` (mainnet/contracts/market/v0-4-market.clar:259) in a state where it convert a rounding direction into a repeatable extraction? Given that it folds accrual over the position's debt list only, the invariant that no position row exists that the position mask does not represent breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:259` -> `accrue-user-debts`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `accrue-user-debts` folds accrual over the position's debt list only. Reach it through `redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the vault's available liquidity relative to the redemption across its boundary values through `redeem` in simnet and assert `accrue-user-debts` never returns a value that breaks the invariant.
