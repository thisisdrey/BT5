# Q0312: new: out-of-bounds read from a crafted length field

## Question
Can an unprivileged attacker reach `new` (in `libsigner/src/http.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a length under/over-runs a buffer, breaking the invariant that every field read stays within the validated message bounds — leading to memory disclosure / crash?

## Target
- File/function: `libsigner/src/http.rs` -> `new`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a length under/over-runs a buffer
- Invariant to test: every field read stays within the validated message bounds
- Expected Immunefi impact: Critical - memory disclosure / crash
- Fast validation: test an OOB length
