# Q4914: new_getsigner: signer event stream surfaces a message no owner signed

## Question
Can an unprivileged attacker reach `new_getsigner` (in `stackslib/src/net/api/getsigner.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `libsigner/http.rs`/`events.rs` parses before checking origin, breaking the invariant that every surfaced message == one an authorized slot owner signed — leading to forged signer input?

## Target
- File/function: `stackslib/src/net/api/getsigner.rs` -> `new_getsigner`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `libsigner/http.rs`/`events.rs` parses before checking origin
- Invariant to test: every surfaced message == one an authorized slot owner signed
- Expected Immunefi impact: Critical - forged signer input
- Fast validation: test an unsigned event
