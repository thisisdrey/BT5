# Q3212: view serialisation diverging from execution semantics — utils.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, state that serialises differently through the view types than through the execution types, with trailing bytes appended after a valid encoding, and additionally with a non-minimal length prefix, reach `create_receipt_id_from_receipt_id` in `core/primitives/src/utils.rs` and have public RPC report a value that contradicts committed state, breaking the invariant that view representations are lossless projections of execution state, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/primitives/src/utils.rs` :: `create_receipt_id_from_receipt_id`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: state that serialises differently through the view types than through the execution types; with trailing bytes appended after a valid encoding; with a non-minimal length prefix
- Exploit idea: have public RPC report a value that contradicts committed state
- Invariant to test: view representations are lossless projections of execution state
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: differential test comparing view output against raw state
