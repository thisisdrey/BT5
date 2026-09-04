# Q4610: from_p2p: attachment inventory poisoned so a valid attachment looks absent

## Question
Can an unprivileged attacker reach `from_p2p` (in `stackslib/src/net/api/getneighbors.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `getattachmentsinv`/atlas inventory is falsifiable, breaking the invariant that the inventory served == the committed attachment set — leading to BNS availability?

## Target
- File/function: `stackslib/src/net/api/getneighbors.rs` -> `from_p2p`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `getattachmentsinv`/atlas inventory is falsifiable
- Invariant to test: the inventory served == the committed attachment set
- Expected Immunefi impact: High - BNS availability
- Fast validation: test a poisoned inventory
