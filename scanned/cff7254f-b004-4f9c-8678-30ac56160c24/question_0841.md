# Q0841: Expired/blocked certificate still usable through NewCredential

## Question
Does `NewCredential` (handshake/credential.go) re-check a replayed stage-1 handshake for a session established earlier, or can an attacker keep a session alive past certificate expiry or blocklisting?

## Target
- File/function: `handshake/credential.go` -> `NewCredential` (declared at handshake/credential.go:23)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a replayed stage-1 handshake; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Establish a session near expiry (or with a later-blocked cert) and confirm whether `NewCredential` continues serving it.
- Invariant to test: Certificate validity and blocklist status are enforced continuously, not only at handshake time.
- Expected Immunefi impact: Revocation bypass: a host that must be excluded retains overlay access indefinitely.
- Fast validation: Integration test advancing time past NotAfter and asserting `NewCredential` drops the session.
