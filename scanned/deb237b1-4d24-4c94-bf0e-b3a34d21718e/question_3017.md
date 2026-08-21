# Q3017: Index collision handling in generateIndex

## Question
Can an attacker force a handshake carrying an empty certificate field so `generateIndex` (handshake_manager.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `handshake_manager.go` -> `generateIndex` (declared at handshake_manager.go:624)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `generateIndex` and asserting the established entry survives and the new one is rejected.
