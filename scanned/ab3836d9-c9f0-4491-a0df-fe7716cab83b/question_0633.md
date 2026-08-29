# Q0633: total-debt via transfer: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752), controlling the timing relative to a pledge or a liquidation, drive `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) — which computes cumulative debt from `principal-scaled` and `index` — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `transfer` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `total-debt` touches, run `transfer` with the timing relative to a pledge or a liquidation, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
