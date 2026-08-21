# Q0751: Route lookup manipulation in parseUnsafeRoutes

## Question
Can an attacker influence an unsafe_route destination so `parseUnsafeRoutes` (overlay/route.go) selects a route or gateway other than the configured one?

## Target
- File/function: `overlay/route.go` -> `parseUnsafeRoutes` (declared at overlay/route.go:149)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an unsafe_route destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `parseUnsafeRoutes` returns the configured route for every attacker-shaped input.
