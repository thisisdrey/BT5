# Q0441: set_runloop_signal_handler: false inventory makes the node skip a canonical tenure

## Question
Can an unprivileged attacker reach `set_runloop_signal_handler` (in `libsigner/src/runloop.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `inv/nakamoto.rs`/download state machine trusts advertised inv, breaking the invariant that the tenure fetched for a slot == the canonical tenure for it — leading to node stalls behind the tip?

## Target
- File/function: `libsigner/src/runloop.rs` -> `set_runloop_signal_handler`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `inv/nakamoto.rs`/download state machine trusts advertised inv
- Invariant to test: the tenure fetched for a slot == the canonical tenure for it
- Expected Immunefi impact: High - node stalls behind the tip
- Fast validation: test a false inventory advert
