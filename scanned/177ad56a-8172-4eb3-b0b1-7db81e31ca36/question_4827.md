# Q4827: send-underlying via deposit: make two code sites that must agree disagree by an attacke

## Question
`send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) pushes the underlying under an `as-contract?` post-condition scope. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing `amount`, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that conversions never round in the user's favour in either direction and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `deposit` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `send-underlying` touches, run `deposit` with `amount`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
