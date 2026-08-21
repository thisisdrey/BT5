# Q2490: Integer overflow in offset math in ipv4PseudoheaderChecksum

## Question
Can arithmetic on attacker-controlled header.Subtype inside `ipv4PseudoheaderChecksum` (iputil/packet.go) overflow or wrap on 32-bit builds so a bounds check passes for an out-of-range offset?

## Target
- File/function: `iputil/packet.go` -> `ipv4PseudoheaderChecksum` (declared at iputil/packet.go:507)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Subtype; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Choose field values near the 32-bit boundary so `offset+length` wraps and compares as small.
- Invariant to test: All offset arithmetic is performed in a width that cannot wrap, or is checked with subtraction against the buffer length.
- Expected Immunefi impact: Remote crash or out-of-bounds read on 32-bit nodes from a single unauthenticated packet.
- Fast validation: Unit test compiled with GOARCH=386 feeding boundary values into `ipv4PseudoheaderChecksum` and asserting rejection rather than a panic.
