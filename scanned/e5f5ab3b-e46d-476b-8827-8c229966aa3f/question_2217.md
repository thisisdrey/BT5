# Q2217: Panic on device error path in findRemovedRoutes

## Question
Can attacker-paced an MTU-sized packet forcing a split write drive `findRemovedRoutes` (overlay/tun.go) into a device error path that panics or leaves the interface unusable?

## Target
- File/function: `overlay/tun.go` -> `findRemovedRoutes` (declared at overlay/tun.go:65)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force write failures under attacker-controlled load and observe recovery.
- Invariant to test: Device errors are handled without panic and the interface recovers or reports cleanly.
- Expected Immunefi impact: Persistent node outage triggered by remote traffic.
- Fast validation: Fault-injection test returning errors from the device write in `findRemovedRoutes`, asserting no panic and continued operation.
