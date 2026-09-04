# Q4358: new: mempool tx stored via a relay path skipping admission

## Question
Can an unprivileged attacker reach `new` (in `stackslib/src/net/api/getmicroblocks_indexed.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that a relayed tx bypasses `will_admit_mempool_tx`, breaking the invariant that every stored tx == one that passed admission — leading to mempool poisoning?

## Target
- File/function: `stackslib/src/net/api/getmicroblocks_indexed.rs` -> `new`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: a relayed tx bypasses `will_admit_mempool_tx`
- Invariant to test: every stored tx == one that passed admission
- Expected Immunefi impact: High - mempool poisoning
- Fast validation: test a relay path insertion
