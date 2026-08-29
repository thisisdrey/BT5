# Q4452: refresh via repay: convert a rounding direction into a repeatable extraction

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls whether the repaid asset is in the accrued debt list reach `refresh` (mainnet/contracts/market/v0-market-vault.clar:171) in a state where it convert a rounding direction into a repeatable extraction? Given that it rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:171` -> `refresh`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `refresh` rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write. Reach it through `repay` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz whether the repaid asset is in the accrued debt list across its boundary values through `repay` in simnet and assert `refresh` never returns a value that breaks the invariant.
