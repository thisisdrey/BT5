# Q2568: iter-find-superset via liquidate: make two code sites that must agree disagree by an attacke

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `debt-amount` reach `iter-find-superset` (mainnet/contracts/registry/v0-egroup.clar:267) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it short-circuits on the first superset match, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:267` -> `iter-find-superset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `debt-amount`
- Exploit idea: `iter-find-superset` short-circuits on the first superset match. Reach it through `liquidate` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `debt-amount` across its boundary values through `liquidate` in simnet and assert `iter-find-superset` never returns a value that breaks the invariant.
