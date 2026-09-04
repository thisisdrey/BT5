# Q2779: decode_atlas_get_attachment: attachment served does not match its content hash

## Question
Can an unprivileged attacker reach `decode_atlas_get_attachment` (in `stackslib/src/net/api/getattachment.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `getattachment`/atlas serves bytes != the requested hash, breaking the invariant that attachment bytes served == bytes whose hash a name op committed — leading to BNS resolution poisoning?

## Target
- File/function: `stackslib/src/net/api/getattachment.rs` -> `decode_atlas_get_attachment`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `getattachment`/atlas serves bytes != the requested hash
- Invariant to test: attachment bytes served == bytes whose hash a name op committed
- Expected Immunefi impact: High - BNS resolution poisoning
- Fast validation: test a mismatched attachment
