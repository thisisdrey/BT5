# Q2668: eth transaction signature binding to the wallet owner — types.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, an RLP-encoded eth transaction whose recovered address differs from the wallet's stored owner, when the recovered address differs from the wallet's stored owner, and additionally when the identical signed payload is relayed twice, reach `construct_public_key` in `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` and get the wallet contract to execute an action for an address it does not belong to, breaking the invariant that the wallet executes only actions signed by the key deriving its own eth-implicit address, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` :: `construct_public_key`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: an RLP-encoded eth transaction whose recovered address differs from the wallet's stored owner; when the recovered address differs from the wallet's stored owner; when the identical signed payload is relayed twice
- Exploit idea: get the wallet contract to execute an action for an address it does not belong to
- Invariant to test: the wallet executes only actions signed by the key deriving its own eth-implicit address
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: wallet-contract unit test with a mismatched recovered address
