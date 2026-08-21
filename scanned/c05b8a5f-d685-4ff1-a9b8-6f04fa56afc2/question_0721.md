# Q0721: Unauthenticated state mutation before verification in H.Parse

## Question
Can an unprivileged attacker with no CA-signed certificate reach state-mutating logic inside `H.Parse` (header/header.go) using a 0-byte datagram, before any cryptographic verification of the sender has completed?

## Target
- File/function: `header/header.go` -> `H.Parse` (declared at header/header.go:143)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a 0-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the packet type that routes into `H.Parse` from a source that has never handshaked, and check whether hostmap, index, or timer state changes.
- Invariant to test: No unauthenticated packet mutates hostmap, index, session, or timer state.
- Expected Immunefi impact: Remote state poisoning / denial of service against an established tunnel by an attacker who holds no certificate.
- Fast validation: Drive `H.Parse` with a synthetic packet in an integration test using the e2e router harness and assert hostmap and index tables are byte-identical before and after.
