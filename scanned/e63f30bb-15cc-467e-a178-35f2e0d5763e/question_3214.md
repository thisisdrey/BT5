# Q3214: zip via borrow: turn an accounting residue into a permanently unclosable p

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `amount`, can an unprivileged attacker make `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) turn an accounting residue into a permanently unclosable position? `zip` pairs the utilization and rate point lists element by element, so the invariant that conversions never round in the user's favour in either direction would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `borrow` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `borrow` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
