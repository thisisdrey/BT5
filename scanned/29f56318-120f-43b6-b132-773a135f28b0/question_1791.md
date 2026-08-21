# Q1791: Index collision handling in connectionManager.resetRelayTrafficCheck

## Question
Can an attacker force a stage-2 handshake for an index never issued so `connectionManager.resetRelayTrafficCheck` (connection_manager.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `connection_manager.go` -> `connectionManager.resetRelayTrafficCheck` (declared at connection_manager.go:196)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `connectionManager.resetRelayTrafficCheck` and asserting the established entry survives and the new one is rejected.
