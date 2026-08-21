# Q2631: Route lookup manipulation in addInterfaceFilter

## Question
Can an attacker influence a route with an attacker-influenced metric so `addInterfaceFilter` (wfp/wfp_windows.go) selects a route or gateway other than the configured one?

## Target
- File/function: `wfp/wfp_windows.go` -> `addInterfaceFilter` (declared at wfp/wfp_windows.go:292)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `addInterfaceFilter` returns the configured route for every attacker-shaped input.
