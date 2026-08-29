# Q0561: get-position via borrow: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling the `ft` trait principal, drive `get-position` (mainnet/contracts/market/v0-4-market.clar:466) — which returns only rows whose bit is set in the ENABLED bitmap — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:466` -> `get-position`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `get-position` returns only rows whose bit is set in the ENABLED bitmap. Reach it through `borrow` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `get-position` touches, run `borrow` with the `ft` trait principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
