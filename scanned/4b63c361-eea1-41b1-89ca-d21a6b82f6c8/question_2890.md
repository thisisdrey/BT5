# Q2890: Stale entry reuse in BalancePacket

## Question
After a peer disconnects, can an attacker use a relay request for a host it does not own so `BalancePacket` (routing/balance.go) reuses the stale entry to bind a new session to the old identity?

## Target
- File/function: `routing/balance.go` -> `BalancePacket` (declared at routing/balance.go:27)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Disconnect a peer, then handshake into the residual entry from a different identity.
- Invariant to test: Teardown fully removes identity, key, and address bindings before any reuse.
- Expected Immunefi impact: Identity takeover of a recently disconnected overlay host.
- Fast validation: Integration test disconnecting then re-binding via `BalancePacket`, asserting no stale identity is inherited.
