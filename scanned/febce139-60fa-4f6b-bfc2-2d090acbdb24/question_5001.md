# Q5001: new_get_sortition: false inventory makes the node skip a canonical tenure

## Question
Can an unprivileged attacker reach `new_get_sortition` (in `stackslib/src/net/api/getsortition.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `inv/nakamoto.rs`/download state machine trusts advertised inv, breaking the invariant that the tenure fetched for a slot == the canonical tenure for it — leading to node stalls behind the tip?

## Target
- File/function: `stackslib/src/net/api/getsortition.rs` -> `new_get_sortition`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `inv/nakamoto.rs`/download state machine trusts advertised inv
- Invariant to test: the tenure fetched for a slot == the canonical tenure for it
- Expected Immunefi impact: High - node stalls behind the tip
- Fast validation: test a false inventory advert
