# Q2928: add-user-collateral via borrow: make two code sites that must agree disagree by an attacke

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls `receiver`, including a contract principal reach `add-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:198) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it adds to the collateral row with a graceful u0 default, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:198` -> `add-user-collateral`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `add-user-collateral` adds to the collateral row with a graceful u0 default. Reach it through `borrow` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `borrow` in simnet and assert `add-user-collateral` never returns a value that breaks the invariant.
