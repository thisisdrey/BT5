# Q2990: send-tokens via repay: turn an accounting residue into a permanently unclosable p

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling `amount`, including far above the real debt (the capping path), can an unprivileged attacker make `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) turn an accounting residue into a permanently unclosable position? `send-tokens` pushes an asset to a caller-chosen recipient principal, so the invariant that conversions never round in the user's favour in either direction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `repay` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `amount`, including far above the real debt (the capping path) varied, and assert that the value `send-tokens` returns is identical in both runs; a divergence confirms the finding.
