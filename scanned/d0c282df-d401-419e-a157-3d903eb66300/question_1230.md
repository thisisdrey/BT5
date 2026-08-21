# Q1230: Unknown-value default-allow in H.TypeName

## Question
Does `H.TypeName` (header/header.go) fall through to a permissive or default branch when an attacker supplies an IPv4 packet with IHL=15 and no options, instead of failing closed and dropping the packet?

## Target
- File/function: `header/header.go` -> `H.TypeName` (declared at header/header.go:159)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv4 packet with IHL=15 and no options; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `H.TypeName` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `H.TypeName` returns an error for every value not explicitly supported.
