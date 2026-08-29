# Q2250: get-account-scaled-debt via repay: compose two individually correct mechanisms into an incorr

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling whether the repaid asset is in the accrued debt list, can an unprivileged attacker make `get-account-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:307) compose two individually correct mechanisms into an incorrect result? `get-account-scaled-debt` reads one scaled debt row, so the invariant that no position row exists that the position mask does not represent would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:307` -> `get-account-scaled-debt`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `get-account-scaled-debt` reads one scaled debt row. Reach it through `repay` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz whether the repaid asset is in the accrued debt list across its boundary values through `repay` in simnet and assert `get-account-scaled-debt` never returns a value that breaks the invariant.
