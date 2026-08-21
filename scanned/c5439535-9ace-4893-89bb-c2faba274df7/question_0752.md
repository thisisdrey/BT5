# Q0752: Route lookup manipulation in ipWithin

## Question
Can an attacker influence an inner packet destined outside the certificate's networks so `ipWithin` (overlay/route.go) selects a route or gateway other than the configured one?

## Target
- File/function: `overlay/route.go` -> `ipWithin` (declared at overlay/route.go:312)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an inner packet destined outside the certificate's networks; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `ipWithin` returns the configured route for every attacker-shaped input.
