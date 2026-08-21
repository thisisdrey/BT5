# Q3574: Unsafe-route policy gap in RemoteAllowList.AllowAll

## Question
Does `RemoteAllowList.AllowAll` (allow_list.go) apply the same rule evaluation to an ICMP inner packet destined for an unsafe_route as it does to overlay-local destinations?

## Target
- File/function: `allow_list.go` -> `RemoteAllowList.AllowAll` (declared at allow_list.go:284)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: an ICMP inner packet; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic addressed to a configured unsafe_route destination and compare the decision path.
- Invariant to test: Unsafe-route traffic is subject to the identical firewall evaluation as overlay traffic.
- Expected Immunefi impact: Pivot from the overlay into the operator's physical network past the firewall.
- Fast validation: Integration test sending denied traffic to an unsafe_route and asserting it never reaches the tun write.
