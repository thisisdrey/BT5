# Q0739: current_miner: attachment served does not match its content hash

## Question
Can an unprivileged attacker reach `current_miner` (in `libsigner/src/v0/messages.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `getattachment`/atlas serves bytes != the requested hash, breaking the invariant that attachment bytes served == bytes whose hash a name op committed — leading to BNS resolution poisoning?

## Target
- File/function: `libsigner/src/v0/messages.rs` -> `current_miner`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `getattachment`/atlas serves bytes != the requested hash
- Invariant to test: attachment bytes served == bytes whose hash a name op committed
- Expected Immunefi impact: High - BNS resolution poisoning
- Fast validation: test a mismatched attachment
