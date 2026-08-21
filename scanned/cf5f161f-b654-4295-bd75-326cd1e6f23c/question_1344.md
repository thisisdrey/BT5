# Q1344: Expired/blocked certificate still usable through UnmarshalPayload

## Question
Does `UnmarshalPayload` (handshake/payload.go) re-check a stage-2 handshake for an index never issued for a session established earlier, or can an attacker keep a session alive past certificate expiry or blocklisting?

## Target
- File/function: `handshake/payload.go` -> `UnmarshalPayload` (declared at handshake/payload.go:68)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Establish a session near expiry (or with a later-blocked cert) and confirm whether `UnmarshalPayload` continues serving it.
- Invariant to test: Certificate validity and blocklist status are enforced continuously, not only at handshake time.
- Expected Immunefi impact: Revocation bypass: a host that must be excluded retains overlay access indefinitely.
- Fast validation: Integration test advancing time past NotAfter and asserting `UnmarshalPayload` drops the session.
