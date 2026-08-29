# Q2748: status via collateral-remove: make two code sites that must agree disagree by an attacke

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls `receiver`, including a contract principal reach `status` (mainnet/contracts/registry/v0-assets.clar:115) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it derives `collateral` and `debt` flags from bit tests against whatever mask it was handed, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:115` -> `status`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `status` derives `collateral` and `debt` flags from bit tests against whatever mask it was handed. Reach it through `collateral-remove` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `collateral-remove` in simnet and assert `status` never returns a value that breaks the invariant.
