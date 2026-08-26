# Q4758: DelegateAction inner nonce vs outer transaction nonce — delegate.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, an inner delegate nonce lower than the sender key's current nonce while the outer relayer nonce is fresh, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `max_block_height` in `core/primitives/src/action/delegate.rs` and advance the outer transaction while the inner nonce check is skipped or evaluated against the relayer's key, breaking the invariant that the inner delegate nonce is checked against the delegate signer's own access key, not the relayer's, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/action/delegate.rs` :: `max_block_height`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: an inner delegate nonce lower than the sender key's current nonce while the outer relayer nonce is fresh; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: advance the outer transaction while the inner nonce check is skipped or evaluated against the relayer's key
- Invariant to test: the inner delegate nonce is checked against the delegate signer's own access key, not the relayer's
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test with distinct relayer and signer keys asserting DelegateActionInvalidNonce
