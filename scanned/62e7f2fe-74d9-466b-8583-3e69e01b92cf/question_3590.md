# Q3590: vault-system-borrow via liquidate: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling which collateral and debt asset pair is targeted, can an unprivileged attacker make `vault-system-borrow` (mainnet/contracts/market/v0-4-market.clar:198) turn an accounting residue into a permanently unclosable position? `vault-system-borrow` routes a borrow to one of six vaults by asset id, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:198` -> `vault-system-borrow`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `vault-system-borrow` routes a borrow to one of six vaults by asset id. Reach it through `liquidate` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with which collateral and debt asset pair is targeted varied, and assert that the value `vault-system-borrow` returns is identical in both runs; a divergence confirms the finding.
