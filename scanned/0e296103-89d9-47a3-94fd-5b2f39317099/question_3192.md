# Q3192: Panic on malformed input path in H.TypeName

## Question
Can an inner protocol byte of 0xFF reach `H.TypeName` (header/header.go) and trigger a nil dereference, slice bound panic, or type assertion failure in the hot receive path?

## Target
- File/function: `header/header.go` -> `H.TypeName` (declared at header/header.go:159)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the receive path with structurally malformed datagrams and observe crashes in `H.TypeName`.
- Invariant to test: The receive path never panics on any byte sequence; malformed input yields a logged drop.
- Expected Immunefi impact: Single-packet remote denial of service of a node, disconnecting all of its tunnels.
- Fast validation: Go fuzz target over `H.TypeName` run to 1M execs with `-race`, asserting zero crashes.
