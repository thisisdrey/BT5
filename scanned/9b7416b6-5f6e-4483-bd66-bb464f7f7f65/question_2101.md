# Q2101: Handshake replay accepted by HandshakeManager.buildStage0Packet

## Question
Can an unprivileged attacker capture and replay a handshake with a Details/Networks mismatch into `HandshakeManager.buildStage0Packet` (handshake_manager.go) to create, revive, or overwrite a session entry?

## Target
- File/function: `handshake_manager.go` -> `HandshakeManager.buildStage0Packet` (declared at handshake_manager.go:650)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a Details/Networks mismatch; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Record a handshake packet off the wire and resend it later from a different source address.
- Invariant to test: A replayed handshake message never produces a usable session or displaces a live one.
- Expected Immunefi impact: Tunnel hijack or denial of service against an established peer by an attacker with no keys.
- Fast validation: Integration test replaying a captured stage-1/stage-2 packet and asserting the existing session's remote and keys are unchanged.
