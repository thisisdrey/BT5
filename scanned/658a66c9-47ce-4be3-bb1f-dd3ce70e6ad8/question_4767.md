# Q4767: total-supply-preview via accrue: make two code sites that must agree disagree by an attacke

## Question
`total-supply-preview` (mainnet/contracts/vault/v0-vault-stx.clar:363) adds the not-yet-minted `calc-treasury-lp-preview` to the live supply that both conversions price against. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing the block time at which accrual is first triggered in a block, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that a value cached within a block still describes the state it was derived from and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:363` -> `total-supply-preview`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `total-supply-preview` adds the not-yet-minted `calc-treasury-lp-preview` to the live supply that both conversions price against. Reach it through `accrue` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `total-supply-preview` touches, run `accrue` with the block time at which accrual is first triggered in a block, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
