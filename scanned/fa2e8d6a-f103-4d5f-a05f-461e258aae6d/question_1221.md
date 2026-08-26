# Q1221: integer representation of balances in views — trie_key.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, balances near u128::MAX and values that lose precision in the view representation, with trailing bytes appended after a valid encoding, reach `access_key_key_len` in `core/primitives-core/src/trie_key.rs` and make an integration read a balance that differs from the protocol value, breaking the invariant that balances are represented losslessly in every serialised form, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives-core/src/trie_key.rs` :: `access_key_key_len`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: balances near u128::MAX and values that lose precision in the view representation; with trailing bytes appended after a valid encoding
- Exploit idea: make an integration read a balance that differs from the protocol value
- Invariant to test: balances are represented losslessly in every serialised form
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: round-trip test over extreme balance values
