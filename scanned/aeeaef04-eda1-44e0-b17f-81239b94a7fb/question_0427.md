# Q0427: set_runloop_signal_handler: forged block relayed before validation

## Question
Can an unprivileged attacker reach `set_runloop_signal_handler` (in `libsigner/src/runloop.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `relay.rs`/`unsolicited.rs` forwards a block from a non-winning peer before validating, breaking the invariant that every relayed message == one verified against consensus — leading to network-wide propagation of forged data?

## Target
- File/function: `libsigner/src/runloop.rs` -> `set_runloop_signal_handler`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `relay.rs`/`unsolicited.rs` forwards a block from a non-winning peer before validating
- Invariant to test: every relayed message == one verified against consensus
- Expected Immunefi impact: Critical - network-wide propagation of forged data
- Fast validation: test relaying an unvalidated block
