# Q1195: DelegateAction expiry off-by-one at max_block_height — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, max_block_height exactly equal to, one below, and one above the current block height, with the boundary value chosen exactly at the enforced limit, reach `append_action_deploy_global_contract` in `runtime/runtime/src/receipt_manager.rs` and get a delegate action accepted one block past its declared expiry window, breaking the invariant that a DelegateAction is rejected once block_height exceeds max_block_height, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_deploy_global_contract`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: max_block_height exactly equal to, one below, and one above the current block height; with the boundary value chosen exactly at the enforced limit
- Exploit idea: get a delegate action accepted one block past its declared expiry window
- Invariant to test: a DelegateAction is rejected once block_height exceeds max_block_height
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: boundary unit test on validate_delegate_action_key / expiry comparison
