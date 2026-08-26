# Q3464: nested DelegateAction rejection — signable_message.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, a DelegateAction whose action list itself contains another DelegateAction, wrapped several levels deep, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `is_transaction` in `core/primitives/src/signable_message.rs` and get recursive delegation accepted so authorisation is evaluated against the wrong signer at some depth, breaking the invariant that DelegateAction is never nested inside another DelegateAction, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/signable_message.rs` :: `is_transaction`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: a DelegateAction whose action list itself contains another DelegateAction, wrapped several levels deep; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: get recursive delegation accepted so authorisation is evaluated against the wrong signer at some depth
- Invariant to test: DelegateAction is never nested inside another DelegateAction
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test on validate_delegate_action asserting DelegateActionCantContainNestedOne at every depth
