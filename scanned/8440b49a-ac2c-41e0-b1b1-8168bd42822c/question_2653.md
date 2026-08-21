# Q2653: Certificate details not bound to session in connectionManager.In

## Question
Does `connectionManager.In` (connection_manager.go) bind the verified certificate's identity to the session keys, or can a handshake with a mismatched noise pattern/curve let the identity used for firewall decisions differ from the identity that owns the handshake keys?

## Target
- File/function: `connection_manager.go` -> `connectionManager.In` (declared at connection_manager.go:107)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a mismatched noise pattern/curve; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present a handshake where the certificate embedded in the payload differs from the one whose key completes the noise exchange.
- Invariant to test: The certificate whose public key completes the noise handshake is the exact certificate used for all later firewall and hostmap decisions.
- Expected Immunefi impact: Identity spoofing: attacker traffic is evaluated against another host's groups and networks, bypassing the firewall.
- Fast validation: Integration test swapping the payload certificate after key agreement and asserting `connectionManager.In` rejects the handshake.
