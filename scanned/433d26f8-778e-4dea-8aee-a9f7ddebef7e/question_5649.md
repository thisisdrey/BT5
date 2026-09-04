# Q5649: get_prior_last_sortition_consensus_hash: handshake signature omits the peer address/network

## Question
Can an unprivileged attacker reach `get_prior_last_sortition_consensus_hash` (in `stackslib/src/net/api/gettenureblocks.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a peer impersonates another or crosses networks, breaking the invariant that the peer identity/network bound == the one the signature authenticated — leading to peer impersonation?

## Target
- File/function: `stackslib/src/net/api/gettenureblocks.rs` -> `get_prior_last_sortition_consensus_hash`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a peer impersonates another or crosses networks
- Invariant to test: the peer identity/network bound == the one the signature authenticated
- Expected Immunefi impact: Critical - peer impersonation
- Fast validation: test a handshake over a stripped message
