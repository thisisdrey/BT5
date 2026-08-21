# Q3848: Batch/recv-loop buffer reuse in H.Parse

## Question
Can an unprivileged attacker exploit buffer reuse across iterations in `H.Parse` (header/header.go) with an inner packet claiming 65535 bytes so bytes from one datagram are processed as part of the next?

## Target
- File/function: `header/header.go` -> `H.Parse` (declared at header/header.go:143)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a long datagram followed by a short one and check whether stale bytes from the first are visible to the second's processing.
- Invariant to test: Each iteration processes exactly the bytes returned for that datagram; no residue from prior reads is readable.
- Expected Immunefi impact: Cross-packet data leakage or firewall bypass by carrying a previously accepted header into a new packet.
- Fast validation: Integration test on the recv loop alternating long and short datagrams, asserting parsed content matches each datagram exactly.
