# Q2880: refresh via borrow: make two code sites that must agree disagree by an attacke

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls `receiver`, including a contract principal reach `refresh` (mainnet/contracts/market/v0-market-vault.clar:171) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:171` -> `refresh`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `refresh` rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write. Reach it through `borrow` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `borrow` in simnet and assert `refresh` never returns a value that breaks the invariant.
