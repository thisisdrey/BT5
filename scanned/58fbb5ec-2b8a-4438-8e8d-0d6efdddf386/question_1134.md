# Q1134: Unknown-value default-allow in Interface.readOutsidePackets

## Question
Does `Interface.readOutsidePackets` (outside.go) fall through to a permissive or default branch when an attacker supplies header.RemoteIndex, instead of failing closed and dropping the packet?

## Target
- File/function: `outside.go` -> `Interface.readOutsidePackets` (declared at outside.go:26)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.RemoteIndex; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `Interface.readOutsidePackets` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `Interface.readOutsidePackets` returns an error for every value not explicitly supported.
