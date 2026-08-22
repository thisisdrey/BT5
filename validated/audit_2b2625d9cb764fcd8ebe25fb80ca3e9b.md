### Title
Unauthenticated `RecvError` packet spoofing allows remote attackers to force tunnel teardown, bypassing all cryptographic authentication - (File: `outside.go`)

### Summary
The external report describes a Proposal Store contract whose `AddProposal()` function has no access control, letting anyone mutate protected governance state directly instead of going through the intended, authenticated voting/execution flow. The Nebula analog is `handleRecvError` in `outside.go`: it is invoked for `header.RecvError` packets *before* any handshake, certificate, or AEAD/ciphertext verification takes place, and its only "authentication" is comparing the spoofable UDP source address against the last known remote endpoint of a hostinfo. This lets an attacker with no CA-signed certificate directly mutate protected tunnel state (tear down an established tunnel) by forging a single small UDP packet.

### Finding Description
In `readOutsidePackets`, message types are dispatched by header before decryption: [1](#0-0) 

`header.RecvError` packets are routed straight to `f.handleRecvError(via.UdpAddr, h)` with zero cryptographic verification — no handshake, no certificate, no CA-pool check, and no AEAD authentication of the packet contents, unlike every other authenticated message type (`Message`, `LightHouse`, `Test`, `CloseTunnel`, `Control`) which all require a successfully `Decrypt`ed payload first.

`handleRecvError` itself: [2](#0-1) 

The function:
1. Looks up a hostinfo purely from `h.RemoteIndex` — a 32-bit value that is transmitted in cleartext in the header of *every* Nebula packet on that tunnel (`header.Encode(..., index, ...)`), so any passive observer/attacker on the network path can learn it without ever completing a handshake or holding a valid certificate.
2. "Validates" the sender only by checking that the UDP source address of the RecvError packet equals `hostinfo.GetRemote()` — plain UDP source-address matching, which is trivially forgeable by any off-path/on-path attacker (no cryptographic binding, no cookie, no signature).
3. If both weak checks pass, it directly mutates protected state: `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` — deleting the tunnel outright, exactly analogous to `AddProposal()` mutating the ProposalStore state without any real authorization gate.

This mirrors the code4rena finding's root cause: a state-mutating entry point that is supposed to be reachable only by an authenticated/verified party (the voting/governance process there; the legitimate cryptographically-authenticated peer here) but is in fact reachable by anyone who can send a raw packet with a guessable/observable identifier and a spoofed source address.

### Impact Explanation
An attacker without any CA-signed certificate — someone who cannot complete a Nebula handshake at all — can still tear down arbitrary active tunnels between legitimate nodes by:
- Sniffing or inferring a victim's `RemoteIndex` (sent unencrypted on the wire), and
- Spoofing the UDP source address of the legitimate remote peer.

This causes remote state poisoning (deletion of `HostInfo` entries from the hostmap and handshake manager) and denial of service, forcing repeated re-handshakes and potential loss of availability for the mesh — a materially similar "bypass the intended access-control/authentication process" impact to the code4rena finding, which the judge rated High Severity because normal protections (voting/access control) were completely skipped.

### Likelihood Explanation
Reachable pre-authentication and requires no valid certificate, handshake completion, or decryption capability — only knowledge of a `RemoteIndex` (observable from cleartext headers on any packet of the target tunnel, e.g., via passive sniffing on a shared/untrusted network segment) and the ability to spoof a UDP source address, both realistic for a network-adjacent or on-path attacker. This is gated behind `acceptRecvErrorConfig` (which can be configured to `recvErrorNever`/`recvErrorPrivate`), so likelihood depends on deployment configuration, but with the default/`recvErrorAlways` setting the path is fully open to spoofing.

### Recommendation
Do not allow protected state mutation (`closeTunnel`/`DeleteHostInfo`) based solely on UDP source-address matching and a cleartext index. Either:
- Require `RecvError` handling to only take effect if it can be tied to a currently valid conntrack/handshake context with additional entropy not observable off-tunnel, or
- Rate-limit/require corroborating evidence (e.g., only accept if a subsequent data packet also fails, or bind an authenticated MAC over the RecvError payload using existing session keys) before tearing down the tunnel, or
- Default `acceptRecvErrorConfig` to a stricter mode and clearly document that `recvErrorAlways` exposes tunnels to spoofed-teardown DoS.

### Proof of Concept
1. Establish a legitimate tunnel between `Victim-A` and `Victim-B`; passively capture a packet on the wire and record `h.RemoteIndex` from the cleartext header (per `header.Encode`/`h.Parse` used in `readOutsidePackets`, `outside.go:25-26`).
2. As an attacker with no CA-issued certificate, craft a bare `header.RecvError` packet (`header.Len`-sized) with that `RemoteIndex`, and spoof the UDP source address to match `Victim-B`'s current remote address as seen by `Victim-A` (`hostinfo.GetRemote()`).
3. Send this forged packet to `Victim-A`. `readOutsidePackets` routes it straight to `handleRecvError` (`outside.go:82`) without any decryption/authentication.
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex(h.RemoteIndex)`, sees the spoofed source address matches `hr`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` (`outside.go:563-574`), tearing down the tunnel without either peer having done anything wrong — a direct, unauthenticated state-mutation analogous to the anyone-can-call `AddProposal()` bug.

*(Note: I was not able to fully verify within tool-call limits how strictly `via.UdpAddr` is validated upstream by the UDP listener against IP spoofing at the OS/network layer, which could affect exploitability on some deployments; this is a gap in my investigation, not a claim that mitigates the finding.)*

### Citations

**File:** outside.go (L75-84)
```go
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
