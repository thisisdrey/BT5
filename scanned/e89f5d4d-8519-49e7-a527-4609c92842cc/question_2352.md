# Q2352: socialize-debt via repay: make two code sites that must agree disagree by an attacke

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `on-behalf-of`, naming any third-party principal reach `socialize-debt` (mainnet/contracts/vault/v0-vault-stx.clar:944) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it writes down `lindex` by one ratio while reducing `assets` by a completely different `principal-reduction`, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:944` -> `socialize-debt`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `on-behalf-of`, naming any third-party principal
- Exploit idea: `socialize-debt` writes down `lindex` by one ratio while reducing `assets` by a completely different `principal-reduction`. Reach it through `repay` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `on-behalf-of`, naming any third-party principal across its boundary values through `repay` in simnet and assert `socialize-debt` never returns a value that breaks the invariant.
