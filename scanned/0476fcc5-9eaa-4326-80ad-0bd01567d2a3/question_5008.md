# Q5008: new_get_sortition: StackerDB slot-to-owner mapping read from the wrong cycle

## Question
Can an unprivileged attacker reach `new_get_sortition` (in `stackslib/src/net/api/getsortition.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `stackerdb/config.rs` maps a slot using a stale reward cycle, breaking the invariant that the owner enforced for a slot == the owner for the current cycle — leading to cross-cycle slot takeover?

## Target
- File/function: `stackslib/src/net/api/getsortition.rs` -> `new_get_sortition`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `stackerdb/config.rs` maps a slot using a stale reward cycle
- Invariant to test: the owner enforced for a slot == the owner for the current cycle
- Expected Immunefi impact: Critical - cross-cycle slot takeover
- Fast validation: test a stale-cycle mapping
