# Q5085: merge-price via borrow: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling `receiver`, including a contract principal, drive `merge-price` (mainnet/contracts/market/v0-4-market.clar:506) — which attaches a price to an asset record by position in the fold, not by asset id — to turn an accounting residue into a permanently unclosable position, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:506` -> `merge-price`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `merge-price` attaches a price to an asset record by position in the fold, not by asset id. Reach it through `borrow` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `merge-price` touches, run `borrow` with `receiver`, including a contract principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
