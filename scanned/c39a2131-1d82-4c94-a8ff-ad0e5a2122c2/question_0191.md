# Q0191: eth transaction nonce replay — types.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, one signed eth transaction relayed twice, and two transactions sharing a nonce, when the recovered address differs from the wallet's stored owner, reach `construct_public_key` in `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` and replay an authorised eth transaction so a transfer executes more than once, breaking the invariant that each eth nonce executes exactly once and strictly increases, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` :: `construct_public_key`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: one signed eth transaction relayed twice, and two transactions sharing a nonce; when the recovered address differs from the wallet's stored owner
- Exploit idea: replay an authorised eth transaction so a transfer executes more than once
- Invariant to test: each eth nonce executes exactly once and strictly increases
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: wallet-contract test relaying the identical payload twice
