# Q4597: from_p2p: forged block relayed before validation

## Question
Can an unprivileged attacker reach `from_p2p` (in `stackslib/src/net/api/getneighbors.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `relay.rs`/`unsolicited.rs` forwards a block from a non-winning peer before validating, breaking the invariant that every relayed message == one verified against consensus — leading to network-wide propagation of forged data?

## Target
- File/function: `stackslib/src/net/api/getneighbors.rs` -> `from_p2p`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `relay.rs`/`unsolicited.rs` forwards a block from a non-winning peer before validating
- Invariant to test: every relayed message == one verified against consensus
- Expected Immunefi impact: Critical - network-wide propagation of forged data
- Fast validation: test relaying an unvalidated block
