# Q3242: DelegateAction replay after relayer submission — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, one signed DelegateAction plus a max_block_height far in the future, resubmitted by several relayers, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `append_action_deploy_contract` in `runtime/runtime/src/receipt_manager.rs` and have the same signed delegate payload executed more than once against the signer account, breaking the invariant that a signed DelegateAction executes at most once regardless of how many relayers submit it, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_deploy_contract`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: one signed DelegateAction plus a max_block_height far in the future, resubmitted by several relayers; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: have the same signed delegate payload executed more than once against the signer account
- Invariant to test: a signed DelegateAction executes at most once regardless of how many relayers submit it
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test submitting the identical SignedDelegateAction twice and asserting the second fails
