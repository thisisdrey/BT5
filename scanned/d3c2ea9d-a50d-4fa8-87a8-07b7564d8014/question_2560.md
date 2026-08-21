# Q2560: PKI reload race in newConnectionStateFromResult

## Question
Can an attacker time a handshake against a certificate/CA reload so `newConnectionStateFromResult` (connection_state.go) validates against a half-swapped trust store while handling a stage-2 handshake for an index never issued?

## Target
- File/function: `connection_state.go` -> `newConnectionStateFromResult` (declared at connection_state.go:45)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a stage-2 handshake for an index never issued; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trigger handshakes continuously during a reload and look for a window where the CA pool is empty or stale in `newConnectionStateFromResult`.
- Invariant to test: Trust-store swaps are atomic; no handshake is ever verified against a partially installed CA pool.
- Expected Immunefi impact: Authentication bypass during reload, admitting an untrusted certificate.
- Fast validation: Concurrency test with `-race` handshaking in a loop while reloading the CA pool, asserting zero untrusted acceptances.
