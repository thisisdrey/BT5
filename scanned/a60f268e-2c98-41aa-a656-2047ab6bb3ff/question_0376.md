# Q0376: Index collision handling in subtypeInfoFor

## Question
Can an attacker force a handshake whose remote index collides with a live one so `subtypeInfoFor` (handshake/patterns.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `handshake/patterns.go` -> `subtypeInfoFor` (declared at handshake/patterns.go:49)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake whose remote index collides with a live one; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `subtypeInfoFor` and asserting the established entry survives and the new one is rejected.
