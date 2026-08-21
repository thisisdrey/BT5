# Q3163: Stale entry reuse in calculatedRemote.ApplyV6

## Question
After a peer disconnects, can an attacker use a HostUpdateNotification for another host's address so `calculatedRemote.ApplyV6` (calculated_remote.go) reuses the stale entry to bind a new session to the old identity?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV6` (declared at calculated_remote.go:59)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Disconnect a peer, then handshake into the residual entry from a different identity.
- Invariant to test: Teardown fully removes identity, key, and address bindings before any reuse.
- Expected Immunefi impact: Identity takeover of a recently disconnected overlay host.
- Fast validation: Integration test disconnecting then re-binding via `calculatedRemote.ApplyV6`, asserting no stale identity is inherited.
