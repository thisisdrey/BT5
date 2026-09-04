# Q5035: new_get_sortition_consensus: relay-hint loop amplifies one message

## Question
Can an unprivileged attacker reach `new_get_sortition_consensus` (in `stackslib/src/net/api/getsortition.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a relay path re-broadcasts without dedup, breaking the invariant that a message is relayed a bounded number of times — leading to amplification?

## Target
- File/function: `stackslib/src/net/api/getsortition.rs` -> `new_get_sortition_consensus`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a relay path re-broadcasts without dedup
- Invariant to test: a message is relayed a bounded number of times
- Expected Immunefi impact: High - amplification
- Fast validation: test a relay loop
