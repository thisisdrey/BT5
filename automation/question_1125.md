# Q1125: replay_txids: integer overflow in a length computation

## Question
Can an unprivileged attacker reach `replay_txids` (in `libsigner/src/v0/messages.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a size math overflows and mis-sizes a read, breaking the invariant that length arithmetic on wire values == checked, non-wrapping — leading to crash / over-read?

## Target
- File/function: `libsigner/src/v0/messages.rs` -> `replay_txids`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a size math overflows and mis-sizes a read
- Invariant to test: length arithmetic on wire values == checked, non-wrapping
- Expected Immunefi impact: Critical - crash / over-read
- Fast validation: test an overflowing length
