# Q0856: get_signer_signature_hash: read-only endpoint runs Clarity past its cost bound

## Question
Can an unprivileged attacker reach `get_signer_signature_hash` (in `libsigner/src/v0/messages.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `fastcallreadonly` limiter resets between sub-calls, breaking the invariant that compute a read endpoint performs <= the configured bound — leading to unauthenticated compute DoS?

## Target
- File/function: `libsigner/src/v0/messages.rs` -> `get_signer_signature_hash`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `fastcallreadonly` limiter resets between sub-calls
- Invariant to test: compute a read endpoint performs <= the configured bound
- Expected Immunefi impact: High - unauthenticated compute DoS
- Fast validation: test an unbounded read-only call
