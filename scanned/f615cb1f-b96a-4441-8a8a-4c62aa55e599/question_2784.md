# Q2784: calc-utilization via collateral-add: make two code sites that must agree disagree by an attacke

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls `amount` reach `calc-utilization` (mainnet/contracts/vault/v0-vault-stx.clar:164) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it divides debt by available liquidity, which can exceed BPS when debt outruns assets, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:164` -> `calc-utilization`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `calc-utilization` divides debt by available liquidity, which can exceed BPS when debt outruns assets. Reach it through `collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `collateral-add` in simnet and assert `calc-utilization` never returns a value that breaks the invariant.
