# Q3653: Rate/size gate missing before expensive work in NewListenConfig

## Question
Can an unprivileged attacker cause `NewListenConfig` (udp/udp_windows.go) to perform expensive per-packet work for an inner protocol byte of 0xFF before any cheap structural validation rejects it?

## Target
- File/function: `udp/udp_windows.go` -> `NewListenConfig` (declared at udp/udp_windows.go:36)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send minimal-cost malformed packets that still reach the expensive branch of `NewListenConfig`.
- Invariant to test: Cheap structural checks precede any allocation, crypto, or map operation on unauthenticated input.
- Expected Immunefi impact: Asymmetric CPU exhaustion of a remote node from low-cost unauthenticated traffic.
- Fast validation: Benchmark comparing per-packet cost of a malformed vs valid packet through `NewListenConfig`; assert the malformed path is cheaper.
