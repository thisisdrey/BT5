# Q2396: find-debt-scaled via borrow: make two code sites that must agree disagree by an attacke

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the `price-feeds` buffers reach `find-debt-scaled` (mainnet/contracts/market/v0-4-market.clar:621) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it returns u0 for an absent asset, making a missing debt row indistinguishable from no debt, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:621` -> `find-debt-scaled`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `find-debt-scaled` returns u0 for an absent asset, making a missing debt row indistinguishable from no debt. Reach it through `borrow` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `borrow` twice with the `price-feeds` buffers varied, and assert that the value `find-debt-scaled` returns is identical in both runs; a divergence confirms the finding.
