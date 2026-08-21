# Q3325: Source-address trust in NewListener

## Question
Does `NewListener` (udp/udp_windows.go) attach authority to the UDP source address of a packet carrying header.Version set to an unknown value, so an attacker who spoofs that address influences a decision reserved for an authenticated peer?

## Target
- File/function: `udp/udp_windows.go` -> `NewListener` (declared at udp/udp_windows.go:14)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Version set to an unknown value; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Spoof the source address of an existing peer and send the packet type handled by `NewListener`.
- Invariant to test: The UDP source address is never a trust input; authority comes only from successful decryption/verification.
- Expected Immunefi impact: Tunnel hijack or redirection of another host's traffic by an off-path attacker.
- Fast validation: Integration test injecting a packet with a peer's source address but no valid session, asserting `NewListener` drops it and leaves the peer's remote unchanged.
