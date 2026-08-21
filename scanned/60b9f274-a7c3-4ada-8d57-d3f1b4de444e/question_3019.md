# Q3019: Index collision handling in HandshakeManager.buildStage0Packet

## Question
Can an attacker force a burst of handshakes from one source address so `HandshakeManager.buildStage0Packet` (handshake_manager.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `handshake_manager.go` -> `HandshakeManager.buildStage0Packet` (declared at handshake_manager.go:650)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a burst of handshakes from one source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `HandshakeManager.buildStage0Packet` and asserting the established entry survives and the new one is rejected.
