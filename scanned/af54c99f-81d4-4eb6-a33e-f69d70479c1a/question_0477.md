# Q0477: spawn: peer inserted into the frontier under an unproven identity

## Question
Can an unprivileged attacker reach `spawn` (in `libsigner/src/runloop.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `net/db.rs` stores a peer identity it did not verify, breaking the invariant that every frontier entry == a peer whose identity was proven — leading to routing poisoning?

## Target
- File/function: `libsigner/src/runloop.rs` -> `spawn`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `net/db.rs` stores a peer identity it did not verify
- Invariant to test: every frontier entry == a peer whose identity was proven
- Expected Immunefi impact: High - routing poisoning
- Fast validation: test an unverified peer insert
