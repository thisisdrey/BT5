# Q1472: Unknown-value default-allow in NewHeader

## Question
Does `NewHeader` (header/header.go) fall through to a permissive or default branch when an attacker supplies an inner packet claiming 65535 bytes, instead of failing closed and dropping the packet?

## Target
- File/function: `header/header.go` -> `NewHeader` (declared at header/header.go:203)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `NewHeader` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `NewHeader` returns an error for every value not explicitly supported.
