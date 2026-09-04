# Q1112: replay_txids: authorization header comparison is normalised or dual-header

## Question
Can an unprivileged attacker reach `replay_txids` (in `libsigner/src/v0/messages.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a case/whitespace or last-wins header slips the check, breaking the invariant that the accepted secret == an exact byte match of the configured secret — leading to auth bypass?

## Target
- File/function: `libsigner/src/v0/messages.rs` -> `replay_txids`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a case/whitespace or last-wins header slips the check
- Invariant to test: the accepted secret == an exact byte match of the configured secret
- Expected Immunefi impact: Critical - auth bypass
- Fast validation: test a normalised/duplicated auth header
