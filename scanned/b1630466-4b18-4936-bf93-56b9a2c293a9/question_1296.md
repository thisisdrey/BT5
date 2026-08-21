# Q1296: Unauthenticated state mutation before verification in GenericConn.LocalAddr

## Question
Can an unprivileged attacker with no CA-signed certificate reach state-mutating logic inside `GenericConn.LocalAddr` (udp/udp_generic.go) using header.MessageCounter, before any cryptographic verification of the sender has completed?

## Target
- File/function: `udp/udp_generic.go` -> `GenericConn.LocalAddr` (declared at udp/udp_generic.go:47)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.MessageCounter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the packet type that routes into `GenericConn.LocalAddr` from a source that has never handshaked, and check whether hostmap, index, or timer state changes.
- Invariant to test: No unauthenticated packet mutates hostmap, index, session, or timer state.
- Expected Immunefi impact: Remote state poisoning / denial of service against an established tunnel by an attacker who holds no certificate.
- Fast validation: Drive `GenericConn.LocalAddr` with a synthetic packet in an integration test using the e2e router harness and assert hostmap and index tables are byte-identical before and after.
