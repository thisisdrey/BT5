# Q5882: DelegateAction inner nonce vs outer transaction nonce — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, an inner delegate nonce lower than the sender key's current nonce while the outer relayer nonce is fresh, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `append_action_add_gas_key_with_full_access` in `runtime/runtime/src/receipt_manager.rs` and advance the outer transaction while the inner nonce check is skipped or evaluated against the relayer's key, breaking the invariant that the inner delegate nonce is checked against the delegate signer's own access key, not the relayer's, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_add_gas_key_with_full_access`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: an inner delegate nonce lower than the sender key's current nonce while the outer relayer nonce is fresh; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: advance the outer transaction while the inner nonce check is skipped or evaluated against the relayer's key
- Invariant to test: the inner delegate nonce is checked against the delegate signer's own access key, not the relayer's
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test with distinct relayer and signer keys asserting DelegateActionInvalidNonce
