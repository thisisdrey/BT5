# Q0202: interpolate-rate via redeem: convert a rounding direction into a repeatable extraction

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling `min-out`, can an unprivileged attacker make `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) convert a rounding direction into a repeatable extraction? `interpolate-rate` interpolates between packed u16 curve points, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `redeem` with `min-out`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
