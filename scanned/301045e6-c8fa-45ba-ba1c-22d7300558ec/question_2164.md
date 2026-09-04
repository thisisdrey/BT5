# Q2164: from_receipt: StackerDB chunk stored without a valid owner signature

## Question
Can an unprivileged attacker reach `from_receipt` (in `stackslib/src/net/api/blockreplay.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that sync stores a gossiped chunk before verifying its signature, breaking the invariant that every stored/relayed chunk == signed by its slot's current owner — leading to unauthorised StackerDB write?

## Target
- File/function: `stackslib/src/net/api/blockreplay.rs` -> `from_receipt`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: sync stores a gossiped chunk before verifying its signature
- Invariant to test: every stored/relayed chunk == signed by its slot's current owner
- Expected Immunefi impact: Critical - unauthorised StackerDB write
- Fast validation: test a gossiped unsigned chunk
