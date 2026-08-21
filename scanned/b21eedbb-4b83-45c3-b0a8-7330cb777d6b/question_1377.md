# Q1377: Unknown-value default-allow in parseV4

## Question
Does `parseV4` (outside.go) fall through to a permissive or default branch when an attacker supplies a fragmented inner packet with offset!=0, instead of failing closed and dropping the packet?

## Target
- File/function: `outside.go` -> `parseV4` (declared at outside.go:390)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a fragmented inner packet with offset!=0; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `parseV4` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `parseV4` returns an error for every value not explicitly supported.
