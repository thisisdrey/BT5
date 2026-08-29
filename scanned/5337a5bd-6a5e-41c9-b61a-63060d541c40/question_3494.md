# Q3494: add-user-scaled-debt via repay: turn an accounting residue into a permanently unclosable p

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling `amount`, including far above the real debt (the capping path), can an unprivileged attacker make `add-user-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:237) turn an accounting residue into a permanently unclosable position? `add-user-scaled-debt` adds to the scaled debt row with a graceful u0 default, so the invariant that conversions never round in the user's favour in either direction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:237` -> `add-user-scaled-debt`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `add-user-scaled-debt` adds to the scaled debt row with a graceful u0 default. Reach it through `repay` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `amount`, including far above the real debt (the capping path) varied, and assert that the value `add-user-scaled-debt` returns is identical in both runs; a divergence confirms the finding.
