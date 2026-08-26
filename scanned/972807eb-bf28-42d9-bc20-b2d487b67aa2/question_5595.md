# Q5595: FunctionCall key allowance vs attached deposit — delegate.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a FunctionCall access key whose allowance is smaller than the fee of the action list it authorises, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `delegate_action` in `core/primitives/src/action/delegate.rs` and get the deposit/fee charged against the account while the allowance check reads a different, smaller quantity, breaking the invariant that the allowance charged equals the total balance the key actually moves, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/action/delegate.rs` :: `delegate_action`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a FunctionCall access key whose allowance is smaller than the fee of the action list it authorises; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: get the deposit/fee charged against the account while the allowance check reads a different, smaller quantity
- Invariant to test: the allowance charged equals the total balance the key actually moves
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test comparing allowance delta against account balance delta for one call
