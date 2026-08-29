# Q3914: filter-u128 via collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling whether this asset is already collateral (the is-new-collateral branch), can an unprivileged attacker make `filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) turn an accounting residue into a permanently unclosable position? `filter-u128` filters a 128-entry bucket list, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: whether this asset is already collateral (the is-new-collateral branch)
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with whether this asset is already collateral (the is-new-collateral branch) varied, and assert that the value `filter-u128` returns is identical in both runs; a divergence confirms the finding.
