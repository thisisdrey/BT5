# Q1656: Unauthenticated state mutation before verification in GenericConn.ListenOut

## Question
Can an unprivileged attacker with no CA-signed certificate reach state-mutating logic inside `GenericConn.ListenOut` (udp/udp_generic.go) using an IPv6 packet with chained extension headers, before any cryptographic verification of the sender has completed?

## Target
- File/function: `udp/udp_generic.go` -> `GenericConn.ListenOut` (declared at udp/udp_generic.go:76)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the packet type that routes into `GenericConn.ListenOut` from a source that has never handshaked, and check whether hostmap, index, or timer state changes.
- Invariant to test: No unauthenticated packet mutates hostmap, index, session, or timer state.
- Expected Immunefi impact: Remote state poisoning / denial of service against an established tunnel by an attacker who holds no certificate.
- Fast validation: Drive `GenericConn.ListenOut` with a synthetic packet in an integration test using the e2e router harness and assert hostmap and index tables are byte-identical before and after.
