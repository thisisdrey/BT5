# Q2033: Index collision handling in newCertState

## Question
Can an attacker force a handshake naming a VPN address already owned so `newCertState` (pki.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `pki.go` -> `newCertState` (declared at pki.go:377)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake naming a VPN address already owned; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `newCertState` and asserting the established entry survives and the new one is rejected.
