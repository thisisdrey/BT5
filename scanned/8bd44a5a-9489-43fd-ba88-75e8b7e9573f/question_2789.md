# Q2789: decode_atlas_get_attachment: callreadonly executes a mutating path via a trait

## Question
Can an unprivileged attacker reach `decode_atlas_get_attachment` (in `stackslib/src/net/api/getattachment.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a read-only RPC call mutates through a trait arg, breaking the invariant that a read-only endpoint performs no state writes — leading to unexpected state change via RPC?

## Target
- File/function: `stackslib/src/net/api/getattachment.rs` -> `decode_atlas_get_attachment`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a read-only RPC call mutates through a trait arg
- Invariant to test: a read-only endpoint performs no state writes
- Expected Immunefi impact: High - unexpected state change via RPC
- Fast validation: test a mutating read-only call
