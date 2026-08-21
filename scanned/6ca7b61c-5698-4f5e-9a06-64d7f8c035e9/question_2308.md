# Q2308: Integer overflow in offset math in Interface.maybeSendRecvError

## Question
Can arithmetic on attacker-controlled an inner packet claiming 65535 bytes inside `Interface.maybeSendRecvError` (outside.go) overflow or wrap on 32-bit builds so a bounds check passes for an out-of-range offset?

## Target
- File/function: `outside.go` -> `Interface.maybeSendRecvError` (declared at outside.go:480)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Choose field values near the 32-bit boundary so `offset+length` wraps and compares as small.
- Invariant to test: All offset arithmetic is performed in a width that cannot wrap, or is checked with subtraction against the buffer length.
- Expected Immunefi impact: Remote crash or out-of-bounds read on 32-bit nodes from a single unauthenticated packet.
- Fast validation: Unit test compiled with GOARCH=386 feeding boundary values into `Interface.maybeSendRecvError` and asserting rejection rather than a panic.
