### Title
Unauthenticated `RecvError` packet allows spoofed remote-state poisoning / tunnel teardown - (File: outside.go)

### Summary
The external report's bug class is that a party without the affected user's consent can force a state change (`userLastDepositTime`) that the user cannot control, denying them normal operation. The Nebula analog is `handleRecvError()` in `outside.go`: it processes an unauthenticated, unencrypted `header.RecvError` packet type and tears down an established tunnel based solely on comparing the UDP source address to the hostinfo's currently recorded remote — a value trivially matched by an off-path attacker who can spoof a UDP source address, with no certificate or handshake authentication required at all.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` before any cert/hostinfo/decryption checks: [1](#0-0) 

`handleRecvError` is then invoked directly with the raw (unauthenticated) source `netip.AddrPort` and the packet's cleartext `RemoteIndex` field: [2](#0-1) 

The only "authentication" performed is:
```
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    // "Someone spoofing recv_errors?"
    return
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
```
This check merely compares the packet's UDP source address against the value Nebula itself already believes is the peer's remote — it does not cryptographically verify the sender. An attacker with no CA-signed Nebula certificate, positioned to spoof UDP packets from the victim's real remote `AddrPort` (e.g. on-path, or off-path networks that don't filter source-address spoofing), can send a bare `RecvError` header packet naming the victim's `RemoteIndex` and force `closeTunnel` + `DeleteHostInfo` on a fully established, authenticated tunnel — exactly mirroring the source report's theme of an unauthorized third party imposing state on a victim without their consent, this time weaponized into a full tunnel teardown/DoS rather than a lockup.

This is reachable pre-authentication: `RecvError` handling occurs in the `switch h.Type` block at `outside.go:76-84`, before hostinfo lookup, decryption, or any certificate validation happens for other packet types (`outside.go:89-136`).

### Impact Explanation
An attacker with no valid certificate can remotely force termination of any active, authenticated Nebula tunnel between two legitimate peers by spoofing a single unencrypted UDP packet, provided they can guess/observe the victim's `RemoteIndex` (a 32-bit value transmitted in cleartext on every packet, and also learnable by an on-path observer) and spoof the peer's known UDP address. This causes denial of service (repeated forced re-handshakes / teardown) against victims who never consented to this state change, analogous to the report's "DoS on the user via state imposed by an unauthorized party."

### Likelihood Explanation
Exploitability depends on the deployment's `acceptRecvErrorConfig` (`listen.send_recv_error`-style setting): `recvErrorAlways` accepts from anyone, `recvErrorPrivate` limits it to private-address peers, `recvErrorNever` disables acceptance entirely (per `recvErrorConfig.ShouldRecvError` in `interface.go`). Additionally, source-address spoofing over UDP is generally only feasible for on-path attackers or in networks without egress/ingress filtering (BCP38); this reduces likelihood for pure off-path internet attackers but remains plausible in many real deployments (shared LANs, cloud VPC sniffing, misconfigured networks). Because `RemoteIndex` and the endpoint are both attacker-controllable/observable and no cryptographic proof of possession of the session key is required, likelihood is Medium.

### Recommendation
- Require the `RecvError` message (or an equivalent signal) to be authenticated, e.g., include a MAC/signature keyed by the session's negotiated key material, or only honor it if it correlates with a recent legitimately-sent packet's counter/nonce.
- Tighten default `acceptRecvErrorConfig` to `recvErrorNever`/`recvErrorPrivate` and clearly document the spoofing risk of `recvErrorAlways`.
- Rate-limit/log repeated `RecvError`-triggered teardowns per remote index to reduce DoS impact, and require additional corroborating signals (e.g., an actual loss of traffic) before tearing down a tunnel purely on an unauthenticated `RecvError`.

### Proof of Concept
1. Establish a legitimate Nebula tunnel between hosts A and B; record B's `RemoteIndex` as seen by A (obtainable via traffic sniffing on a shared segment, or if attacker is on-path).
2. From attacker host C (no CA-signed certificate), craft a single UDP packet with header `Type = header.RecvError`, `RemoteIndex` set to the value A uses for B, and source `AddrPort` spoofed to match B's known UDP endpoint.
3. Send this packet to A.
4. Observe `handleRecvError` on A passes the `hr != addr` check (since the spoofed source matches B's real remote) and calls `f.closeTunnel(hostinfo)` / `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel without any handshake or certificate validation from the attacker. [2](#0-1)

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
