# Q0567: Index collision handling in ConnectionState.NextMessageCounter

## Question
Can an attacker force a handshake with a Details/Networks mismatch so `ConnectionState.NextMessageCounter` (connection_state.go) evicts, overwrites, or aliases a legitimate peer's local index entry?

## Target
- File/function: `connection_state.go` -> `ConnectionState.NextMessageCounter` (declared at connection_state.go:75)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a Details/Networks mismatch; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send handshakes chosen so the generated or claimed index collides with a live session's index.
- Invariant to test: Index allocation and collision handling never evict or alias an established, authenticated session.
- Expected Immunefi impact: Denial of service or session confusion against an established tunnel from an unauthenticated attacker.
- Fast validation: Unit test forcing an index collision through `ConnectionState.NextMessageCounter` and asserting the established entry survives and the new one is rejected.
