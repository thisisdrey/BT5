# Q1451: Index collision handling in connectionManager.reload

## Question
Can an attacker force a handshake carrying an empty certificate field so `connectionManager.reload` (connection_manager.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `connection_manager.go` -> `connectionManager.reload` (declared at connection_manager.go:67)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `connectionManager.reload` and asserting the established entry survives and the new one is rejected.
