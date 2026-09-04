# Q4728: decode_rpc_get_pox_info: fee-rate estimate steered by crafted input

## Question
Can an unprivileged attacker reach `decode_rpc_get_pox_info` (in `stackslib/src/net/api/getpoxinfo.rs`) via a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory), such that `postfeerate` returns an attacker-influenced estimate, breaking the invariant that the estimate returned == a function of canonical state only — leading to fee manipulation?

## Target
- File/function: `stackslib/src/net/api/getpoxinfo.rs` -> `decode_rpc_get_pox_info`
- Entrypoint: a remote TCP connection to a node's P2P or RPC port carrying attacker-chosen bytes (handshake, gossiped block/tx, HTTP request, StackerDB chunk, Atlas attachment, advertised inventory)
- Attacker controls: the full byte content of every message and request, their own peer identity, a StackerDB slot they legitimately own, and the timing/ordering of messages
- Exploit idea: `postfeerate` returns an attacker-influenced estimate
- Invariant to test: the estimate returned == a function of canonical state only
- Expected Immunefi impact: High - fee manipulation
- Fast validation: test a crafted fee-rate request
