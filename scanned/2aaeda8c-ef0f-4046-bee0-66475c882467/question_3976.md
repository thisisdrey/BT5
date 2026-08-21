# Q3976: Index/counter confusion in GenericConn.Rebind

## Question
Can an attacker choose header.Type so `GenericConn.Rebind` (udp/udp_bsd.go) selects a session, key, or hostinfo belonging to a different peer than the one that actually sent the packet?

## Target
- File/function: `udp/udp_bsd.go` -> `GenericConn.Rebind` (declared at udp/udp_bsd.go:46)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Type; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a packet whose index/counter fields name another peer's session and see which state `GenericConn.Rebind` resolves.
- Invariant to test: Session lookup is confirmed by successful authenticated decryption before any per-peer state is touched.
- Expected Immunefi impact: Cross-session state confusion enabling traffic injection or teardown of a third party's tunnel.
- Fast validation: Unit test resolving `GenericConn.Rebind` with a foreign index, asserting the packet is dropped and no session is mutated.
