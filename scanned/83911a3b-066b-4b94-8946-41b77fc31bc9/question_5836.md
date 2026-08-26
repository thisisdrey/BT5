# Q5836: DelegateAction expiry off-by-one at max_block_height — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, max_block_height exactly equal to, one below, and one above the current block height, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `append_action_stake` in `runtime/runtime/src/receipt_manager.rs` and get a delegate action accepted one block past its declared expiry window, breaking the invariant that a DelegateAction is rejected once block_height exceeds max_block_height, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_stake`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: max_block_height exactly equal to, one below, and one above the current block height; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: get a delegate action accepted one block past its declared expiry window
- Invariant to test: a DelegateAction is rejected once block_height exceeds max_block_height
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: boundary unit test on validate_delegate_action_key / expiry comparison
