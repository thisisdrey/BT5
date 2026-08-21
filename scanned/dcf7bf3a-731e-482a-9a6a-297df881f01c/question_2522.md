# Q2522: Third-party address advertisement in Punchy.ScheduleRespond

## Question
Can an attacker advertise a HostUpdateNotification for another host's address through `Punchy.ScheduleRespond` (punchy.go) to make a victim node send traffic to an unrelated third party?

## Target
- File/function: `punchy.go` -> `Punchy.ScheduleRespond` (declared at punchy.go:151)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote pointing at a chosen victim IP:port and observe the node's outbound traffic.
- Invariant to test: Advertised remotes are only ever used for the identity that owns them, and are filtered against configured ranges.
- Expected Immunefi impact: Traffic amplification/reflection using nodes as unwitting senders toward a third-party target.
- Fast validation: Integration test asserting `Punchy.ScheduleRespond` never emits packets to an address advertised for a foreign identity.
