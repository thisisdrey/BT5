# Q3483: Source-address trust in GenericConn.Rebind

## Question
Does `GenericConn.Rebind` (udp/udp_android.go) attach authority to the UDP source address of a packet carrying an inner protocol byte of 0xFF, so an attacker who spoofs that address influences a decision reserved for an authenticated peer?

## Target
- File/function: `udp/udp_android.go` -> `GenericConn.Rebind` (declared at udp/udp_android.go:43)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Spoof the source address of an existing peer and send the packet type handled by `GenericConn.Rebind`.
- Invariant to test: The UDP source address is never a trust input; authority comes only from successful decryption/verification.
- Expected Immunefi impact: Tunnel hijack or redirection of another host's traffic by an off-path attacker.
- Fast validation: Integration test injecting a packet with a peer's source address but no valid session, asserting `GenericConn.Rebind` drops it and leaves the peer's remote unchanged.
