# Q3606: zip via call-ststx-ratio: turn an accounting residue into a permanently unclosable p

## Question
Entering through `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) while controlling the block and transaction position at which the external ratio is fetched, can an unprivileged attacker make `zip` (mainnet/contracts/vault/v0-vault-stx.clar:226) turn an accounting residue into a permanently unclosable position? `zip` pairs the utilization and rate point lists element by element, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:226` -> `zip`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: the block and transaction position at which the external ratio is fetched
- Exploit idea: `zip` pairs the utilization and rate point lists element by element. Reach it through `call-ststx-ratio` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the block and transaction position at which the external ratio is fetched across its boundary values through `call-ststx-ratio` in simnet and assert `zip` never returns a value that breaks the invariant.
