# Q3956: decode_peer_info: Preamble length fields reinterpret a later body

## Question
Can an unprivileged attacker reach `decode_peer_info` (in `stackslib/src/net/api/getinfo.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `codec.rs` length lets a following message body be reframed, breaking the invariant that each message body == framed by its own validated length — leading to message confusion / smuggling?

## Target
- File/function: `stackslib/src/net/api/getinfo.rs` -> `decode_peer_info`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `codec.rs` length lets a following message body be reframed
- Invariant to test: each message body == framed by its own validated length
- Expected Immunefi impact: Critical - message confusion / smuggling
- Fast validation: test a crafted preamble length
