# Q2889: get-available-assets via redeem: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), controlling `amount` of shares burned, drive `get-available-assets` (mainnet/contracts/vault/v0-vault-stx.clar:481) — which reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on — to convert a rounding direction into a repeatable extraction, breaking the invariant that conversions never round in the user's favour in either direction, and cause temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:481` -> `get-available-assets`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `amount` of shares burned
- Exploit idea: `get-available-assets` reads real liquidity, a different quantity from the `assets` var that `redeem` also gates on. Reach it through `redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `get-available-assets` touches, run `redeem` with `amount` of shares burned, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
