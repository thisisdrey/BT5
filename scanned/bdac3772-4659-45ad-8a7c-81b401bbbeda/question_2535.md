# Q2535: Unauthenticated state mutation before verification in RIOConn.receive

## Question
Can an unprivileged attacker with no CA-signed certificate reach state-mutating logic inside `RIOConn.receive` (udp/udp_rio_windows.go) using an IPv6 packet with chained extension headers, before any cryptographic verification of the sender has completed?

## Target
- File/function: `udp/udp_rio_windows.go` -> `RIOConn.receive` (declared at udp/udp_rio_windows.go:184)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the packet type that routes into `RIOConn.receive` from a source that has never handshaked, and check whether hostmap, index, or timer state changes.
- Invariant to test: No unauthenticated packet mutates hostmap, index, session, or timer state.
- Expected Immunefi impact: Remote state poisoning / denial of service against an established tunnel by an attacker who holds no certificate.
- Fast validation: Drive `RIOConn.receive` with a synthetic packet in an integration test using the e2e router harness and assert hostmap and index tables are byte-identical before and after.
