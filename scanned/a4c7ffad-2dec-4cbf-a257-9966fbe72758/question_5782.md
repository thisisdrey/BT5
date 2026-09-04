# Q5782: new_get_tenure_blocks: download state machine wedged by a crafted advert

## Question
Can an unprivileged attacker reach `new_get_tenure_blocks` (in `stackslib/src/net/api/gettenureblocks.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that the tenure downloader loops or stalls, breaking the invariant that the downloader reaches the tip in bounded time — leading to liveness / availability?

## Target
- File/function: `stackslib/src/net/api/gettenureblocks.rs` -> `new_get_tenure_blocks`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: the tenure downloader loops or stalls
- Invariant to test: the downloader reaches the tip in bounded time
- Expected Immunefi impact: High - liveness / availability
- Fast validation: test a wedging advert
