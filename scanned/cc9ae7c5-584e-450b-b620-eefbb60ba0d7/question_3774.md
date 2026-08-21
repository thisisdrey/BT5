# Q3774: Panic on device error path in Interface.reloadAcceptRecvError

## Question
Can attacker-paced a multicast/broadcast inner destination drive `Interface.reloadAcceptRecvError` (interface.go) into a device error path that panics or leaves the interface unusable?

## Target
- File/function: `interface.go` -> `Interface.reloadAcceptRecvError` (declared at interface.go:459)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a multicast/broadcast inner destination; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force write failures under attacker-controlled load and observe recovery.
- Invariant to test: Device errors are handled without panic and the interface recovers or reports cleanly.
- Expected Immunefi impact: Persistent node outage triggered by remote traffic.
- Fast validation: Fault-injection test returning errors from the device write in `Interface.reloadAcceptRecvError`, asserting no panic and continued operation.
