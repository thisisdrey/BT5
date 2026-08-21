# Q3565: Rate/size gate missing before expensive work in NewListener

## Question
Can an unprivileged attacker cause `NewListener` (udp/udp_android.go) to perform expensive per-packet work for a message type outside the known enum before any cheap structural validation rejects it?

## Target
- File/function: `udp/udp_android.go` -> `NewListener` (declared at udp/udp_android.go:16)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a message type outside the known enum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send minimal-cost malformed packets that still reach the expensive branch of `NewListener`.
- Invariant to test: Cheap structural checks precede any allocation, crypto, or map operation on unauthenticated input.
- Expected Immunefi impact: Asymmetric CPU exhaustion of a remote node from low-cost unauthenticated traffic.
- Fast validation: Benchmark comparing per-packet cost of a malformed vs valid packet through `NewListener`; assert the malformed path is cheaper.
