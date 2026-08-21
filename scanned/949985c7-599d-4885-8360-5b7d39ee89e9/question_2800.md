# Q2800: Stale entry reuse in hashPacket

## Question
After a peer disconnects, can an attacker use an advertised remote pointing at a third party so `hashPacket` (routing/balance.go) reuses the stale entry to bind a new session to the old identity?

## Target
- File/function: `routing/balance.go` -> `hashPacket` (declared at routing/balance.go:14)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised remote pointing at a third party; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Disconnect a peer, then handshake into the residual entry from a different identity.
- Invariant to test: Teardown fully removes identity, key, and address bindings before any reuse.
- Expected Immunefi impact: Identity takeover of a recently disconnected overlay host.
- Fast validation: Integration test disconnecting then re-binding via `hashPacket`, asserting no stale identity is inherited.
