# Q3940: decode_peer_info: handshake replay processed as a new authenticated frame

## Question
Can an unprivileged attacker reach `decode_peer_info` (in `stackslib/src/net/api/getinfo.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a nonce/sequence accepted out of order, breaking the invariant that each authenticated frame == processed once in order — leading to replay / session hijack?

## Target
- File/function: `stackslib/src/net/api/getinfo.rs` -> `decode_peer_info`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a nonce/sequence accepted out of order
- Invariant to test: each authenticated frame == processed once in order
- Expected Immunefi impact: Critical - replay / session hijack
- Fast validation: test a replayed handshake
