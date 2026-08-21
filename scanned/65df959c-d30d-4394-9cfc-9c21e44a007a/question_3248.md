# Q3248: Stale entry reuse in newCalculatedRemotesEntryFromConfig

## Question
After a peer disconnects, can an attacker use a duplicate/looping relay chain so `newCalculatedRemotesEntryFromConfig` (calculated_remote.go) reuses the stale entry to bind a new session to the old identity?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesEntryFromConfig` (declared at calculated_remote.go:126)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a duplicate/looping relay chain; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Disconnect a peer, then handshake into the residual entry from a different identity.
- Invariant to test: Teardown fully removes identity, key, and address bindings before any reuse.
- Expected Immunefi impact: Identity takeover of a recently disconnected overlay host.
- Fast validation: Integration test disconnecting then re-binding via `newCalculatedRemotesEntryFromConfig`, asserting no stale identity is inherited.
