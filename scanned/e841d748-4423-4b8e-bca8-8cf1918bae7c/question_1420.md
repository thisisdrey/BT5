# Q1420: Source-address trust in StdConn.PrepareRawMessages

## Question
Does `StdConn.PrepareRawMessages` (udp/udp_linux_64.go) attach authority to the UDP source address of a packet carrying header.Version set to an unknown value, so an attacker who spoofs that address influences a decision reserved for an authenticated peer?

## Target
- File/function: `udp/udp_linux_64.go` -> `StdConn.PrepareRawMessages` (declared at udp/udp_linux_64.go:36)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Version set to an unknown value; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Spoof the source address of an existing peer and send the packet type handled by `StdConn.PrepareRawMessages`.
- Invariant to test: The UDP source address is never a trust input; authority comes only from successful decryption/verification.
- Expected Immunefi impact: Tunnel hijack or redirection of another host's traffic by an off-path attacker.
- Fast validation: Integration test injecting a packet with a peer's source address but no valid session, asserting `StdConn.PrepareRawMessages` drops it and leaves the peer's remote unchanged.
