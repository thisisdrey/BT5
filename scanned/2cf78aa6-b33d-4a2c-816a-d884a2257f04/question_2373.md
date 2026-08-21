# Q2373: Expired/blocked certificate still usable through ConnectionState.MarshalJSON

## Question
Does `ConnectionState.MarshalJSON` (connection_state.go) re-check a stage-2 handshake for an index never issued for a session established earlier, or can an attacker keep a session alive past certificate expiry or blocklisting?

## Target
- File/function: `connection_state.go` -> `ConnectionState.MarshalJSON` (declared at connection_state.go:66)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Establish a session near expiry (or with a later-blocked cert) and confirm whether `ConnectionState.MarshalJSON` continues serving it.
- Invariant to test: Certificate validity and blocklist status are enforced continuously, not only at handshake time.
- Expected Immunefi impact: Revocation bypass: a host that must be excluded retains overlay access indefinitely.
- Fast validation: Integration test advancing time past NotAfter and asserting `ConnectionState.MarshalJSON` drops the session.
