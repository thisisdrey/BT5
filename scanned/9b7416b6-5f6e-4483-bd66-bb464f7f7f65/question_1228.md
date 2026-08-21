# Q1228: Unknown-value default-allow in H.Encode

## Question
Does `H.Encode` (header/header.go) fall through to a permissive or default branch when an attacker supplies a 0-byte datagram, instead of failing closed and dropping the packet?

## Target
- File/function: `header/header.go` -> `H.Encode` (declared at header/header.go:134)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a 0-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `H.Encode` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `H.Encode` returns an error for every value not explicitly supported.
