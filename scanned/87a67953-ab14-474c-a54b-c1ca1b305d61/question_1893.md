# Q1893: Session teardown reachable by unauthenticated packet via ConnectionState.Curve

## Question
Can a replayed stage-1 handshake reach `ConnectionState.Curve` (connection_state.go) and cause an established, authenticated session to be closed, reset, or marked for re-handshake without the attacker proving key possession?

## Target
- File/function: `connection_state.go` -> `ConnectionState.Curve` (declared at connection_state.go:84)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a replayed stage-1 handshake; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the teardown/recv-error style packet naming a live session index from an unrelated source.
- Invariant to test: Session teardown is acted on only after authenticated decryption proves the sender holds the session keys.
- Expected Immunefi impact: Denial of service tearing down another host's tunnel from an unprivileged network position.
- Fast validation: Integration test sending an unauthenticated teardown for a live session, asserting the session survives.
