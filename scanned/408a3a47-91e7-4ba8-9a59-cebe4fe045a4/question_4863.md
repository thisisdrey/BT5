# Q4863: new: broadcast flag parsed so the authed path runs unauthenticated

## Question
Can an unprivileged attacker reach `new` (in `stackslib/src/net/api/getsigner.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `broadcast=1` parsing lets the authenticated branch run without the secret, breaking the invariant that the authenticated action == only on a valid secret — leading to auth bypass?

## Target
- File/function: `stackslib/src/net/api/getsigner.rs` -> `new`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `broadcast=1` parsing lets the authenticated branch run without the secret
- Invariant to test: the authenticated action == only on a valid secret
- Expected Immunefi impact: Critical - auth bypass
- Fast validation: test a crafted broadcast flag
