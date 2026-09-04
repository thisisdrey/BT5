# Q2191: new: auth-gated endpoint fails open when no secret is configured

## Question
Can an unprivileged attacker reach `new` (in `stackslib/src/net/api/blockreplay.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a missing `auth_token`/password is treated as 'allow' rather than 'disabled', breaking the invariant that requests that execute the gated action == those carrying the exact configured secret — leading to unauthenticated privileged action?

## Target
- File/function: `stackslib/src/net/api/blockreplay.rs` -> `new`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a missing `auth_token`/password is treated as 'allow' rather than 'disabled'
- Invariant to test: requests that execute the gated action == those carrying the exact configured secret
- Expected Immunefi impact: Critical - unauthenticated privileged action
- Fast validation: net test posting to the endpoint with no secret
