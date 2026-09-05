# Q1530: insert_update: HTTP request body size not bounded before buffering

## Question
Can an unprivileged attacker reach `insert_update` (in `libsigner/src/v0/signer_state.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `httpcore.rs`/`request.rs` buffers an unbounded body, breaking the invariant that bytes buffered for a request <= the configured cap — leading to memory pressure from one request?

## Target
- File/function: `libsigner/src/v0/signer_state.rs` -> `insert_update`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `httpcore.rs`/`request.rs` buffers an unbounded body
- Invariant to test: bytes buffered for a request <= the configured cap
- Expected Immunefi impact: High - memory pressure from one request
- Fast validation: test an oversized body
