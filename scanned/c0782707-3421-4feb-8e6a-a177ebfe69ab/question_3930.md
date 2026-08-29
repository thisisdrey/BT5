# Q3930: receive-underlying via accrue: satisfy a bound with a value the bound was never designed 

## Question
Entering through `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) while controlling the block time at which accrual is first triggered in a block, can an unprivileged attacker make `receive-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:291) satisfy a bound with a value the bound was never designed to admit? `receive-underlying` pulls the underlying from a named account, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:291` -> `receive-underlying`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `receive-underlying` pulls the underlying from a named account. Reach it through `accrue` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the block time at which accrual is first triggered in a block across its boundary values through `accrue` in simnet and assert `receive-underlying` never returns a value that breaks the invariant.
