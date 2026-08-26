# Q5995: NEP-461 domain separation on signable messages — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, a payload whose borsh encoding is valid both as a Transaction and as a signable DelegateAction discriminant, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `append_action_stake` in `runtime/runtime/src/receipt_manager.rs` and reuse one signature across two message types so a signature for message A authorises action B, breaking the invariant that every signed message type has a unique, prefix-committed discriminant, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_stake`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: a payload whose borsh encoding is valid both as a Transaction and as a signable DelegateAction discriminant; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: reuse one signature across two message types so a signature for message A authorises action B
- Invariant to test: every signed message type has a unique, prefix-committed discriminant
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: differential test signing one payload and attempting verification under both message types
