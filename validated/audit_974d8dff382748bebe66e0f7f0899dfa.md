### Title
Unauthenticated `RecvError` packets allow a spoofing attacker to force-teardown established tunnels - (File: `outside.go`)

### Summary
Nebula's `handleRecvError` path processes `header.RecvError` packets before any Noise handshake or certificate verification occurs, and it authorizes the teardown solely by comparing the UDP source address on the incoming packet to the tunnel's known remote address. Because UDP carries no source-address authentication, an attacker without a CA-signed certificate can spoof this comparison and force `closeTunnel`/`DeleteHostInfo` on a live, mutually-authenticated tunnel. This mirrors the reported ERC777 bug class: an unauthenticated, attacker-controlled signal reaching a critical state-transition path (tearing down a security-critical, already-established channel) that should require authenticated participation, effectively "blocking" the legitimate operation (the ongoing encrypted session) the same way a malicious token callback blocks liquidation.

### Finding Description
`readOutsidePackets` in `outside.go` dispatches `header.RecvError` packets straight to `f.handleRecvError(via.UdpAddr, h)` prior to any decryption or certificate check: [1](#0-0) 

`handleRecvError` itself performs no cryptographic authentication of the sender. It only checks a config toggle, looks up the hostinfo by the (unauthenticated, attacker-supplied) `RemoteIndex`, and then compares the *claimed* source `netip.AddrPort` (`addr`, taken directly off the received UDP datagram, which is trivially spoofable at the IP layer for this connectionless comparison) against the stored remote address of that hostinfo: [2](#0-1) 

If the spoofed source matches (`hr == addr`), the code proceeds to `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` — fully tearing down an already-authenticated, encrypted tunnel between two legitimately certificate-holding peers, triggered entirely by a packet from someone holding no CA-signed certificate at all.

The `RemoteIndex` used to look up the hostinfo (`h.RemoteIndex`) is not a secret: it is exchanged in cleartext handshake packets and is also observable to any on-path or off-path attacker capable of address spoofing, so the barrier to constructing a valid-looking `RecvError` teardown packet is low. There is a same-file comment/log ("Someone spoofing recv_errors?") acknowledging the maintainers are aware source-spoofing is possible here, but the mitigation is only a log line — the tunnel is still torn down if the spoofed source matches.

### Impact Explanation
This is a remote, unauthenticated denial-of-service against an already-secured VPN tunnel: an attacker who never presents a CA-signed certificate can force termination of any active Nebula tunnel whose external UDP endpoint and handshake index it can observe/guess, causing repeated reconnection churn or persistent disruption of legitimate encrypted traffic — directly analogous to the ERC777 report's "block liquidation" impact (an unauthenticated external signal disrupting a security-critical, already-in-flight operation).

### Likelihood Explanation
Exploitability depends on the attacker's ability to spoof a UDP source address matching the victim's stored remote endpoint and to know/guess the `RemoteIndex` for a target tunnel — both of which are feasible for a network-adjacent or off-path attacker capable of IP spoofing (common on many networks lacking BCP38 filtering), and the feature is enabled with `always` accept behavior by default per the project's own changelog entry adding a config knob to control it (#1569).

### Recommendation
Do not let `RecvError` acceptance be decided by an unauthenticated source-address string comparison alone. At minimum, require the sender to be authenticated for the tunnel being torn down (e.g., accept `RecvError` only when it can be validated against the current Noise/AEAD session state, or ignore it as a pure "hint" that triggers a bounded re-handshake attempt rather than an unconditional `closeTunnel`/`DeleteHostInfo`). Consider defaulting `accept_recv_error` handling to a rate-limited, non-destructive re-handshake nudge instead of an authoritative teardown signal.

### Proof of Concept
1. Establish two Nebula nodes, A and B, with valid CA-signed certificates and a live tunnel.
2. As an attacker C (holding no Nebula certificate) on a network permitting source IP spoofing, observe/derive the `RemoteIndex` A uses for the A↔B tunnel (obtainable from cleartext handshake headers) and spoof C's UDP source address/port to match B's known endpoint as seen by A.
3. Send a `header.RecvError` packet (as built by `sendRecvError`, i.e. `header.Encode(..., header.RecvError, 0, index, 0)`) to A with that spoofed source.
4. Observe `handleRecvError` on A: `hr.IsValid() && hr != addr` evaluates false (spoofed addr matches), so `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` execute, tearing down the legitimate tunnel without any certificate or Noise-session validation. [3](#0-2)

### Citations

**File:** outside.go (L76-84)
```go
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
