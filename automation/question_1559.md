# Q1559: is_empty: callreadonly executes a mutating path via a trait

## Question
Can an unprivileged attacker reach `is_empty` (in `libsigner/src/v0/signer_state.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a read-only RPC call mutates through a trait arg, breaking the invariant that a read-only endpoint performs no state writes — leading to unexpected state change via RPC?

## Target
- File/function: `libsigner/src/v0/signer_state.rs` -> `is_empty`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a read-only RPC call mutates through a trait arg
- Invariant to test: a read-only endpoint performs no state writes
- Expected Immunefi impact: High - unexpected state change via RPC
- Fast validation: test a mutating read-only call
