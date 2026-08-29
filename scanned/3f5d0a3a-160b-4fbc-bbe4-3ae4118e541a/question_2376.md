# Q2376: receive-underlying via transfer: make two code sites that must agree disagree by an attacke

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls `amount` reach `receive-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:291) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it pulls the underlying from a named account, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:291` -> `receive-underlying`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `receive-underlying` pulls the underlying from a named account. Reach it through `transfer` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `amount` across its boundary values through `transfer` in simnet and assert `receive-underlying` never returns a value that breaks the invariant.
