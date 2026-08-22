This is the strongest reachable analog: `handleRecvError` in `outside.go` processes `header.RecvError` packets before any decryption/authentication — the only guard is a source-IP match against the tunnel's currently-known remote (`hr != addr`), which is trivially satisfiable by an attacker who has learned or guessed a peer's UDP endpoint, since UDP allows arbitrary source-address spoofing and RecvError carries no cryptographic proof of authorship. [1](#0-0) 

### Title
Unauthenticated `RecvError` teardown allows repeated tunnel-state reset / DoS - (File: outside.go)

### Summary
`readOutsidePackets` dispatches `header.RecvError` to `handleRecvError` before any AEAD authentication, keyed only by an attacker-controlled UDP address comparison, letting an off-path attacker force repeated tunnel teardown of a legitimate session.

### Finding Description
`readOutsidePackets` routes `header.RecvError` straight to `f.handleRecvError(via.UdpAddr, h)` in the "Unencrypted packets" switch, prior to any hostinfo/ConnectionState check or packet decryption [2](#0-1) . `handleRecvError` looks up the hostinfo by `h.RemoteIndex` (a 32-bit value that is transmitted in cleartext on every prior handshake/data packet and thus observable to any network-path attacker) and only rejects the request if the `via.UdpAddr` does not match the *currently known* remote of that hostinfo [3](#0-2) . There is no certificate, no AEAD tag, and no proof that the sender holds the session keys — anything that can (a) learn the `RemoteIndex` value and (b) spoof or relay from the peer's current UDP source address can trigger `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` [4](#0-3) . This mirrors the reported bug class exactly: a cheap, permissionless, unauthenticated request repeatedly resets/destroys legitimate session state (here, the entire tunnel rather than just a cooldown timer), and can be replayed indefinitely to prevent the victim's tunnel from ever staying established, since a fresh handshake immediately triggers a new race the attacker can again terminate.

### Impact Explanation
Any node that can observe or guess the encoded `RemoteIndex` (visible in cleartext on the wire, per `header.H.RemoteIndex`) and spoof the peer's current source `UdpAddr` on UDP can permanently and repeatedly tear down an established tunnel between two legitimate, CA-authenticated nebula nodes — an on-path or off-path (via UDP source spoofing, which is trivial on many networks) attacker achieves persistent Denial of Service without ever presenting a valid certificate. This is a remote, unauthenticated disruption of an already-authenticated session, matching the "remote state poisoning" / DoS class called out in scope.

### Likelihood Explanation
The `RemoteIndex` is not secret — it is exchanged in cleartext handshake and data-plane headers and can be sniffed by any observer of the traffic; the source-address check is the only defense and is defeated by ordinary UDP source spoofing (no return-path validation is required to send a RecvError). Given `listen.send_recv_error`/`accept_recv_error` are enabled by default in many deployments (this feature exists specifically to speed up re-handshaking, per the project's own changelog entry on `send_recv_error`), exploitation only requires basic packet-crafting capability.

### Recommendation
Do not act on `RecvError` packets pre-authentication. At minimum, require the packet to be authenticated using session state that only a party who has completed the handshake, or that already possesses live connection-state material, could produce (e.g., MAC the RecvError with something derived from the existing session, or heavily rate-limit and corroborate RecvError-driven teardown with additional out-of-band evidence such as failed decryption at the alleged sender). Treat `RemoteIndex`-only + spoofable-source authorization as insufficient, the same way the referenced report recommends reviewing permissionless, cheaply-repeatable state-resetting operations.

### Proof of Concept
1. Establish a legitimate tunnel between node A and node B (mutually valid CA certs).
2. Passively observe (or otherwise learn) B's `RemoteIndex` value as sent by A to B (or vice versa) in cleartext headers.
3. From an attacker machine capable of spoofing UDP source addresses as A's current known remote address, send a `header.RecvError` packet to B with `RemoteIndex` set to the value B assigned to A's hostinfo.
4. Observe `handleRecvError` passes the `hr != addr` check (since the spoofed source matches), and B calls `closeTunnel` + `DeleteHostInfo`, tearing down the tunnel.
5. Repeat indefinitely (each new handshake attempt can be torn down the same way) to keep the tunnel between A and B perpetually unavailable — analogous to resetting the cooling period in the original report, but resulting in full, repeatable tunnel destruction rather than a lockout timer.

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
