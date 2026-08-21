# Q1430: Relay loop/chain amplification via newCalculatedRemote

## Question
Can an attacker construct a HostUpdateNotification for another host's address so `newCalculatedRemote` (calculated_remote.go) forwards a packet back into the relay path, creating a loop or multiplying traffic?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemote` (declared at calculated_remote.go:24)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Build a relay chain whose next hop points back at a previous hop.
- Invariant to test: Relay forwarding is depth-limited and never returns a packet to the hop it came from.
- Expected Immunefi impact: Traffic amplification and CPU/bandwidth exhaustion across multiple nodes from one packet.
- Fast validation: Unit test with a looping chain asserting `newCalculatedRemote` drops after one hop.
