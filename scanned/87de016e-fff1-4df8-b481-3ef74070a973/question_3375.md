# Q3375: Panic on malformed input path in Interface.handleRecvError

## Question
Can a truncated 15-byte datagram reach `Interface.handleRecvError` (outside.go) and trigger a nil dereference, slice bound panic, or type assertion failure in the hot receive path?

## Target
- File/function: `outside.go` -> `Interface.handleRecvError` (declared at outside.go:499)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a truncated 15-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the receive path with structurally malformed datagrams and observe crashes in `Interface.handleRecvError`.
- Invariant to test: The receive path never panics on any byte sequence; malformed input yields a logged drop.
- Expected Immunefi impact: Single-packet remote denial of service of a node, disconnecting all of its tunnels.
- Fast validation: Go fuzz target over `Interface.handleRecvError` run to 1M execs with `-race`, asserting zero crashes.
