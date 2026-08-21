# Q1345: Expired/blocked certificate still usable through unmarshalPayloadDetails

## Question
Does `unmarshalPayloadDetails` (handshake/payload.go) re-check a handshake whose remote index collides with a live one for a session established earlier, or can an attacker keep a session alive past certificate expiry or blocklisting?

## Target
- File/function: `handshake/payload.go` -> `unmarshalPayloadDetails` (declared at handshake/payload.go:100)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake whose remote index collides with a live one; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Establish a session near expiry (or with a later-blocked cert) and confirm whether `unmarshalPayloadDetails` continues serving it.
- Invariant to test: Certificate validity and blocklist status are enforced continuously, not only at handshake time.
- Expected Immunefi impact: Revocation bypass: a host that must be excluded retains overlay access indefinitely.
- Fast validation: Integration test advancing time past NotAfter and asserting `unmarshalPayloadDetails` drops the session.
