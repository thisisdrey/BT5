# Q5327: new_getstackers: read endpoint serves state from a non-canonical block

## Question
Can an unprivileged attacker reach `new_getstackers` (in `stackslib/src/net/api/getstackers.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `getmapentry`/`getdatavar`/`getclaritymarfvalue` reads a fork tip as canonical, breaking the invariant that state served == state committed at the requested canonical block — leading to wallets/bridges served wrong state?

## Target
- File/function: `stackslib/src/net/api/getstackers.rs` -> `new_getstackers`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `getmapentry`/`getdatavar`/`getclaritymarfvalue` reads a fork tip as canonical
- Invariant to test: state served == state committed at the requested canonical block
- Expected Immunefi impact: High - wallets/bridges served wrong state
- Fast validation: test a fork-tip query
