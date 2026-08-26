# Q2846: FunctionCall access-key allowance refund inflation — mod.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a FunctionCall key with a tight allowance, a call that fails after burning partial gas, and the resulting gas refund, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `is_delegate` in `core/primitives/src/action/mod.rs` and make the refund credit more allowance back to the key than was deducted, so the key spends more than its allowance overall, breaking the invariant that cumulative gas+deposit spent through a FunctionCall key never exceeds its initial allowance, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/action/mod.rs` :: `is_delegate`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a FunctionCall key with a tight allowance, a call that fails after burning partial gas, and the resulting gas refund; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: make the refund credit more allowance back to the key than was deducted, so the key spends more than its allowance overall
- Invariant to test: cumulative gas+deposit spent through a FunctionCall key never exceeds its initial allowance
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test that loops call+fail+refund and asserts monotonically non-increasing allowance
