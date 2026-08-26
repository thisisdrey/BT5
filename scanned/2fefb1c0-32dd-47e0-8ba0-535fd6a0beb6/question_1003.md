# Q1003: view serialisation diverging from execution semantics — view.rs

## Question
Can an unprivileged mainnet account, entering through a `query` / `call_function` view request on a public RPC node, state that serialises differently through the view types than through the execution types, with trailing bytes appended after a valid encoding, reach `from` in `core/parameters/src/view.rs` and have public RPC report a value that contradicts committed state, breaking the invariant that view representations are lossless projections of execution state, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `core/parameters/src/view.rs` :: `from`
- Entrypoint: a `query` / `call_function` view request on a public RPC node
- Attacker controls: state that serialises differently through the view types than through the execution types; with trailing bytes appended after a valid encoding
- Exploit idea: have public RPC report a value that contradicts committed state
- Invariant to test: view representations are lossless projections of execution state
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: differential test comparing view output against raw state
