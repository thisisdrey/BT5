# Q2467: Expired/blocked certificate still usable through ConnectionState.Decrypt

## Question
Does `ConnectionState.Decrypt` (connection_state.go) re-check a handshake with a Details/Networks mismatch for a session established earlier, or can an attacker keep a session alive past certificate expiry or blocklisting?

## Target
- File/function: `connection_state.go` -> `ConnectionState.Decrypt` (declared at connection_state.go:88)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a Details/Networks mismatch; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Establish a session near expiry (or with a later-blocked cert) and confirm whether `ConnectionState.Decrypt` continues serving it.
- Invariant to test: Certificate validity and blocklist status are enforced continuously, not only at handshake time.
- Expected Immunefi impact: Revocation bypass: a host that must be excluded retains overlay access indefinitely.
- Fast validation: Integration test advancing time past NotAfter and asserting `ConnectionState.Decrypt` drops the session.
