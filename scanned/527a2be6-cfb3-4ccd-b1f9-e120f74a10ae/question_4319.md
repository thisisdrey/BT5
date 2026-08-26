# Q4319: FunctionCall access-key allowance refund inflation — signable_message.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a FunctionCall key with a tight allowance, a call that fails after burning partial gas, and the resulting gas refund, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `is_transaction` in `core/primitives/src/signable_message.rs` and make the refund credit more allowance back to the key than was deducted, so the key spends more than its allowance overall, breaking the invariant that cumulative gas+deposit spent through a FunctionCall key never exceeds its initial allowance, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/signable_message.rs` :: `is_transaction`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a FunctionCall key with a tight allowance, a call that fails after burning partial gas, and the resulting gas refund; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: make the refund credit more allowance back to the key than was deducted, so the key spends more than its allowance overall
- Invariant to test: cumulative gas+deposit spent through a FunctionCall key never exceeds its initial allowance
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test that loops call+fail+refund and asserts monotonically non-increasing allowance
