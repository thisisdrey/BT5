# Q3794: send-tokens via liquidate-multi: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling the full batch list and its ordering, can an unprivileged attacker make `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) turn an accounting residue into a permanently unclosable position? `send-tokens` pushes an asset to a caller-chosen recipient principal, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `liquidate-multi` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate-multi` twice with the full batch list and its ordering varied, and assert that the value `send-tokens` returns is identical in both runs; a divergence confirms the finding.
