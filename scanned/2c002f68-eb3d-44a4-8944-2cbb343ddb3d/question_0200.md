# Q0200: decode_http_body: attachment inventory poisoned so a valid attachment looks absent

## Question
Can an unprivileged attacker reach `decode_http_body` (in `libsigner/src/http.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `getattachmentsinv`/atlas inventory is falsifiable, breaking the invariant that the inventory served == the committed attachment set — leading to BNS availability?

## Target
- File/function: `libsigner/src/http.rs` -> `decode_http_body`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `getattachmentsinv`/atlas inventory is falsifiable
- Invariant to test: the inventory served == the committed attachment set
- Expected Immunefi impact: High - BNS availability
- Fast validation: test a poisoned inventory
