# Q3951: Handshake timeout/retry abuse in CertState.DefaultVersion

## Question
Can an attacker use a handshake carrying an empty certificate field to keep `CertState.DefaultVersion` (pki.go) retrying, re-sending, or holding a slot so a legitimate peer cannot complete its own handshake?

## Target
- File/function: `pki.go` -> `CertState.DefaultVersion` (declared at pki.go:216)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Start handshakes that never complete and observe timer wheel and retry behaviour in `CertState.DefaultVersion`.
- Invariant to test: Incomplete handshakes expire on a fixed schedule and never starve legitimate handshake capacity.
- Expected Immunefi impact: Denial of service preventing legitimate peers from establishing tunnels.
- Fast validation: Integration test with N stalled initiations plus one legitimate peer, asserting the legitimate handshake still completes.
