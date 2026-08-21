# Q1497: Unknown-value default-allow in Interface.maybeSendRecvError

## Question
Does `Interface.maybeSendRecvError` (outside.go) fall through to a permissive or default branch when an attacker supplies a message type outside the known enum, instead of failing closed and dropping the packet?

## Target
- File/function: `outside.go` -> `Interface.maybeSendRecvError` (declared at outside.go:480)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a message type outside the known enum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Enumerate values outside the known enum/range and observe which branch of `Interface.maybeSendRecvError` executes.
- Invariant to test: Unknown or reserved wire values are rejected, never mapped onto a default handler.
- Expected Immunefi impact: Authentication or firewall bypass reached by steering an unauthenticated packet into a handler it should never reach.
- Fast validation: Table-driven unit test over all 256 values of the field, asserting `Interface.maybeSendRecvError` returns an error for every value not explicitly supported.
