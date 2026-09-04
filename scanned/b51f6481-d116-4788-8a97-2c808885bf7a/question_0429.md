# Q0429: set_runloop_signal_handler: handshake signature omits the peer address/network

## Question
Can an unprivileged attacker reach `set_runloop_signal_handler` (in `libsigner/src/runloop.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a peer impersonates another or crosses networks, breaking the invariant that the peer identity/network bound == the one the signature authenticated — leading to peer impersonation?

## Target
- File/function: `libsigner/src/runloop.rs` -> `set_runloop_signal_handler`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a peer impersonates another or crosses networks
- Invariant to test: the peer identity/network bound == the one the signature authenticated
- Expected Immunefi impact: Critical - peer impersonation
- Fast validation: test a handshake over a stripped message
