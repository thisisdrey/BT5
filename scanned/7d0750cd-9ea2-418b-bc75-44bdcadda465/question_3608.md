# Q3608: Unsafe-route policy gap in Interface.getOrHandshakeConsiderRouting

## Question
Does `Interface.getOrHandshakeConsiderRouting` (inside.go) apply the same rule evaluation to the destination port destined for an unsafe_route as it does to overlay-local destinations?

## Target
- File/function: `inside.go` -> `Interface.getOrHandshakeConsiderRouting` (declared at inside.go:146)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the destination port; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic addressed to a configured unsafe_route destination and compare the decision path.
- Invariant to test: Unsafe-route traffic is subject to the identical firewall evaluation as overlay traffic.
- Expected Immunefi impact: Pivot from the overlay into the operator's physical network past the firewall.
- Fast validation: Integration test sending denied traffic to an unsafe_route and asserting it never reaches the tun write.
