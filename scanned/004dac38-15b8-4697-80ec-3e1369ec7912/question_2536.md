# Q2536: Panic on malformed input path in NewListener

## Question
Can an IPv4 packet with IHL=15 and no options reach `NewListener` (udp/udp_windows.go) and trigger a nil dereference, slice bound panic, or type assertion failure in the hot receive path?

## Target
- File/function: `udp/udp_windows.go` -> `NewListener` (declared at udp/udp_windows.go:14)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv4 packet with IHL=15 and no options; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the receive path with structurally malformed datagrams and observe crashes in `NewListener`.
- Invariant to test: The receive path never panics on any byte sequence; malformed input yields a logged drop.
- Expected Immunefi impact: Single-packet remote denial of service of a node, disconnecting all of its tunnels.
- Fast validation: Go fuzz target over `NewListener` run to 1M execs with `-race`, asserting zero crashes.
