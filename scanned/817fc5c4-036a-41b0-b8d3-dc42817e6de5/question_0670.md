# Q0670: Third-party address advertisement in calculatedRemote.ApplyV4

## Question
Can an attacker advertise a HostQueryReply for an unrequested VPN address through `calculatedRemote.ApplyV4` (calculated_remote.go) to make a victim node send traffic to an unrelated third party?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV4` (declared at calculated_remote.go:45)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote pointing at a chosen victim IP:port and observe the node's outbound traffic.
- Invariant to test: Advertised remotes are only ever used for the identity that owns them, and are filtered against configured ranges.
- Expected Immunefi impact: Traffic amplification/reflection using nodes as unwitting senders toward a third-party target.
- Fast validation: Integration test asserting `calculatedRemote.ApplyV4` never emits packets to an address advertised for a foreign identity.
