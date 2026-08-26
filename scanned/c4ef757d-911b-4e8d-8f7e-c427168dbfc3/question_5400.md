# Q5400: eth transaction signature binding to the wallet owner — types.rs

## Question
Can an unprivileged mainnet account, entering through a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id, an RLP-encoded eth transaction whose recovered address differs from the wallet's stored owner, when two payloads share one nonce, and additionally when the chain id belongs to another network, reach `current_account_suffix` in `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` and get the wallet contract to execute an action for an address it does not belong to, breaking the invariant that the wallet executes only actions signed by the key deriving its own eth-implicit address, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/near-wallet-contract/implementation/wallet-contract/src/types.rs` :: `current_account_suffix`
- Entrypoint: a `Transfer` action to a 64-hex implicit or 0x-prefixed eth-implicit account id
- Attacker controls: an RLP-encoded eth transaction whose recovered address differs from the wallet's stored owner; when two payloads share one nonce; when the chain id belongs to another network
- Exploit idea: get the wallet contract to execute an action for an address it does not belong to
- Invariant to test: the wallet executes only actions signed by the key deriving its own eth-implicit address
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: wallet-contract unit test with a mismatched recovered address
