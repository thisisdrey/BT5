# Q1466: PKI reload race in unmarshalPayloadDetails

## Question
Can an attacker time a handshake against a certificate/CA reload so `unmarshalPayloadDetails` (handshake/payload.go) validates against a half-swapped trust store while handling a handshake carrying an empty certificate field?

## Target
- File/function: `handshake/payload.go` -> `unmarshalPayloadDetails` (declared at handshake/payload.go:100)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Trigger handshakes continuously during a reload and look for a window where the CA pool is empty or stale in `unmarshalPayloadDetails`.
- Invariant to test: Trust-store swaps are atomic; no handshake is ever verified against a partially installed CA pool.
- Expected Immunefi impact: Authentication bypass during reload, admitting an untrusted certificate.
- Fast validation: Concurrency test with `-race` handshaking in a loop while reloading the CA pool, asserting zero untrusted acceptances.
