# Q5955: nested DelegateAction rejection — gas.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, a DelegateAction whose action list itself contains another DelegateAction, wrapped several levels deep, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `saturating_add` in `core/primitives-core/src/gas.rs` and get recursive delegation accepted so authorisation is evaluated against the wrong signer at some depth, breaking the invariant that DelegateAction is never nested inside another DelegateAction, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives-core/src/gas.rs` :: `saturating_add`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: a DelegateAction whose action list itself contains another DelegateAction, wrapped several levels deep; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: get recursive delegation accepted so authorisation is evaluated against the wrong signer at some depth
- Invariant to test: DelegateAction is never nested inside another DelegateAction
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test on validate_delegate_action asserting DelegateActionCantContainNestedOne at every depth
