# Q1220: resolve-dia via borrow: make an aggregate and its per-item breakdown disagree

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the `ft` trait principal reach `resolve-dia` (mainnet/contracts/market/v0-4-market.clar:326) in a state where it make an aggregate and its per-item breakdown disagree? Given that it derives a (string-ascii 32) key from a (buff 32) ident, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:326` -> `resolve-dia`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `resolve-dia` derives a (string-ascii 32) key from a (buff 32) ident. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with the `ft` trait principal varied, and assert that the value `resolve-dia` returns is identical in both runs; a divergence confirms the finding.
