# Q1617: NEP-461 domain separation on signable messages — delegate.rs

## Question
Can an unprivileged mainnet account, entering through a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer, a payload whose borsh encoding is valid both as a Transaction and as a signable DelegateAction discriminant, with the boundary value chosen exactly at the enforced limit, reach `get_nep461_hash` in `core/primitives/src/action/delegate.rs` and reuse one signature across two message types so a signature for message A authorises action B, breaking the invariant that every signed message type has a unique, prefix-committed discriminant, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/action/delegate.rs` :: `get_nep461_hash`
- Entrypoint: a `DelegateAction` (NEP-366 meta-transaction) handed to any public relayer
- Attacker controls: a payload whose borsh encoding is valid both as a Transaction and as a signable DelegateAction discriminant; with the boundary value chosen exactly at the enforced limit
- Exploit idea: reuse one signature across two message types so a signature for message A authorises action B
- Invariant to test: every signed message type has a unique, prefix-committed discriminant
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: differential test signing one payload and attempting verification under both message types
