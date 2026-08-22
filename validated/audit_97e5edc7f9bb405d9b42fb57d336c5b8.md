### Title
Unauthenticated `RecvError` packets allow remote attackers to tear down established tunnels using an observable, cleartext index - ([File: outside.go])

### Summary

### Finding Description
The external report's bug class is: a security/administrative control exists (`setEmergencyPaused`) but is not enforced on the critical state-mutating functions (`stake`/`withdraw`), so an attacker can still exercise those functions after the control should have blocked them.

The Nebula analog is the `RecvError` control message and its handler `handleRecvError`. `header.RemoteIndex` travels in the **cleartext** packet header on every single packet Nebula sends [1](#0-0) , so it is not a secret — any observer of the wire traffic between two peers learns it. `RecvError` is processed in the "Unencrypted packets" branch, before any AEAD authentication or certificate/CA verification is performed, alongside `Handshake` packets [2](#0-1) .

The only "authentication" check performed for a `RecvError` packet is a comparison of the packet's UDP source address to the hostinfo's currently known remote `AddrPort`:

```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		...
		return
	}
	...
	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		...
		return
	}

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?", ...)
		return
	}

	f.closeTunnel(hostinfo)
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
``` [3](#0-2) 

This is a UDP source-address check, not a cryptographic one — it is exactly the kind of easily-bypassed "control that isn't enforced with real authentication" that mirrors the missing `whenNotPaused` modifier in the external report: the config knob `f.acceptRecvErrorConfig` (gated by `listen.accept_recv_error`, default `always` per the changelog) [4](#0-3)  is the only gate, and it does not require possession of a CA-signed certificate, valid handshake state, or any AEAD-authenticated proof of identity — only an attacker-controlled UDP source address matching the peer's known endpoint and a `RemoteIndex` value that is trivially observable in cleartext on every packet.

The critical function here — `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, which tears down an active tunnel — is the "withdraw"-equivalent operation: state-mutating and high-impact, yet reachable without any of the certificate/handshake authentication machinery that protects the rest of the data path (e.g. `firewall.Drop`, `ConnectionState.Decrypt`, `handshake.Machine.ProcessPacket`).

### Impact Explanation
An attacker with no CA-signed certificate who can spoof UDP source IP/port to match a target's known remote endpoint (trivial for UDP, and easier still for an on-path or off-path attacker who can observe/predict the source port) and who can observe the cleartext `RemoteIndex` from any packet on that flow (via passive sniffing on a shared network segment, a compromised intermediate router, or simply being the responder side and reading the packet the responder receives) can forge a `RecvError` packet that unilaterally destroys the victim's tunnel state. This is a persistent remote denial-of-service against a specific Nebula tunnel: repeated forged `RecvError` packets can keep tearing the tunnel down as fast as it can re-handshake, disrupting connectivity for the mesh overlay.

### Likelihood Explanation
Likelihood is moderate to high in permissive network positions: the check is purely address-based (no crypto), the identifier used to target a hostinfo (`RemoteIndex`) is unauthenticated and sent in cleartext on the wire, and the default configuration (`accept_recv_error: always`) accepts these packets from anyone whose source address matches. UDP source spoofing is well understood and, at minimum, any attacker who can observe traffic between the two Nebula peers (e.g., shared LAN, malicious/compromised router, or a NAT/firewall observation point) satisfies both preconditions without needing a Nebula certificate at all.

### Recommendation
Do not allow a bare, unauthenticated `RecvError` packet to unilaterally tear down tunnel state. At minimum:
- Require some proof tied to the session (e.g., an authenticated counter/nonce or a MAC derived from the tunnel's established keys) before acting on a `RecvError`, rather than relying solely on UDP source-address matching.
- Consider rate-limiting/backoff on `RecvError`-triggered teardown per hostinfo, and/or requiring multiple corroborating signals (e.g., repeated data-plane failures) before tearing down an active tunnel.
- Re-evaluate whether `listen.accept_recv_error` should default to a more restrictive mode (e.g., `private`/`never`) rather than `always`, and document the residual spoofing risk clearly for operators who set `always`.

### Proof of Concept
1. Observe (or otherwise learn) the cleartext `RemoteIndex` of an active Nebula tunnel between hosts A and B by capturing any packet A sends to B (the index is unencrypted in the header) [1](#0-0) .
2. Craft a UDP packet with header `Type = header.RecvError`, `RemoteIndex` set to the observed value, and destination = host B's listener.
3. Spoof the packet's source UDP address/port to match A's known endpoint as seen by B (`hostinfo.GetRemote()`).
4. Send the packet to B. `readOutsidePackets` dispatches it to `handleRecvError` without any decryption/authentication [5](#0-4) ; the address check passes because the spoofed source matches, and B calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying the tunnel [6](#0-5) .
5. Repeat to persistently disrupt the tunnel (DoS), all without ever presenting a CA-signed certificate or completing a handshake.

Note: I was not able to fully trace every downstream consumer of `RemoteIndex` allocation/lifecycle (e.g., how predictable/guessable index values are for an attacker who cannot passively observe traffic at all, purely blind), so the "off-path, no visibility" variant of this attack is less certain; the on-path/observing variant is directly supported by the code shown above.

### Citations

**File:** outside.go (L25-41)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := h.Parse(packet)
	if err != nil {
		// Hole punch packets are 0 or 1 byte big, so lets ignore printing those errors
		// TODO: record metrics for rx holepunch/punchy packets?
		if len(packet) > 1 {
			f.messageMetrics.RxInvalid(1)
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				f.l.Debug("Error while parsing inbound packet",
					"from", via,
					"error", err,
					"packet", packet,
				)
			}
		}
		return
	}
```

**File:** outside.go (L70-84)
```go
	// don't keep Rx metrics for message type, since you can see those in the tun metrics
	if h.Type != header.Message {
		f.messageMetrics.Rx(h.Type, h.Subtype, 1)
	}

	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L541-575)
```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		f.l.Debug("Recv error received, ignoring",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
		return
	}

	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error received",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
	}

	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		f.l.Debug("Did not find remote index in main hostmap", "remoteIndex", h.RemoteIndex)
		return
	}

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?",
			"addr", addr,
			"hostinfoRemote", hr,
		)
		return
	}

	f.closeTunnel(hostinfo)
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```

**File:** CHANGELOG.md (L128-130)
```markdown
### Added

- Add a config option to control accepting `recv_error` packets which defaults to `always`. (#1569)
```
