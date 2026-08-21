# Q0878: Unauthenticated state mutation before verification in parseV6

## Question
Can an unprivileged attacker with no CA-signed certificate reach state-mutating logic inside `parseV6` (outside.go) using an IPv4 packet with IHL=15 and no options, before any cryptographic verification of the sender has completed?

## Target
- File/function: `outside.go` -> `parseV6` (declared at outside.go:321)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv4 packet with IHL=15 and no options; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the packet type that routes into `parseV6` from a source that has never handshaked, and check whether hostmap, index, or timer state changes.
- Invariant to test: No unauthenticated packet mutates hostmap, index, session, or timer state.
- Expected Immunefi impact: Remote state poisoning / denial of service against an established tunnel by an attacker who holds no certificate.
- Fast validation: Drive `parseV6` with a synthetic packet in an integration test using the e2e router harness and assert hostmap and index tables are byte-identical before and after.
