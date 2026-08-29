# Q3026: get-available-assets via deposit: turn an accounting residue into a permanently unclosable p

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling the vault's supply and asset state at the moment of the call, can an unprivileged attacker make `get-available-assets` (mainnet/contracts/vault/v0-vault-stx.clar:481) turn an accounting residue into a permanently unclosable position? `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:481` -> `get-available-assets`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: the vault's supply and asset state at the moment of the call
- Exploit idea: `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on. Reach it through `deposit` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `deposit` twice with the vault's supply and asset state at the moment of the call varied, and assert that the value `get-available-assets` returns is identical in both runs; a divergence confirms the finding.
