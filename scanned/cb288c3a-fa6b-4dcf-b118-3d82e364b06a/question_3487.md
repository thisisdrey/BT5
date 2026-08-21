# Q3487: Unauthenticated state mutation before verification in StdConn.ListenOut

## Question
Can an unprivileged attacker with no CA-signed certificate reach state-mutating logic inside `StdConn.ListenOut` (udp/udp_linux.go) using an inner packet claiming 65535 bytes, before any cryptographic verification of the sender has completed?

## Target
- File/function: `udp/udp_linux.go` -> `StdConn.ListenOut` (declared at udp/udp_linux.go:165)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the packet type that routes into `StdConn.ListenOut` from a source that has never handshaked, and check whether hostmap, index, or timer state changes.
- Invariant to test: No unauthenticated packet mutates hostmap, index, session, or timer state.
- Expected Immunefi impact: Remote state poisoning / denial of service against an established tunnel by an attacker who holds no certificate.
- Fast validation: Drive `StdConn.ListenOut` with a synthetic packet in an integration test using the e2e router harness and assert hostmap and index tables are byte-identical before and after.
