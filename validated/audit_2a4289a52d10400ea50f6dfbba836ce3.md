### Title
Unauthenticated `RecvError` packet allows source-spoofed remote tunnel teardown - ([File: outside.go])

### Summary
The LPS-1 report describes a class of bug where an action that should be restricted to a specific, cryptographically-verified actor (the depositor) can instead be triggered by any arbitrary, unauthorized third party, causing state that belongs to another party to be mutated/stolen. The closest reachable analog in this Nebula codebase is the handling of the `header.RecvError` packet type in `outside.go`: it is processed entirely outside of the AEAD/certificate-authenticated handshake and data path, and the only "authorization" check it performs is a plaintext UDP source-address comparison, which is not a cryptographic proof of peer identity.

### Finding Description
`readOutsidePackets` treats `header.RecvError` (along with `header.Handshake`) as one of the two packet types that bypass all cryptographic authentication (`ConnectionState.Decrypt`/AEAD) entirely: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely by the plaintext `RemoteIndex` field carried in the (unencrypted) packet header, and the only gate before tearing down the tunnel is comparing the UDP source address of the received packet against the `hostinfo.GetRemote()` value: [2](#0-1) 

Both pieces of data an attacker needs to forge this packet are non-secret:
- `RemoteIndex` is sent in the cleartext header of every packet exchanged with a peer (it is never encrypted), so any on-path observer, or even a party who merely receives normal traffic from the victim, can learn it.
- The UDP source `addr` check is a comparison of the *claimed* source address in the received datagram, not a cryptographic binding — UDP allows source-address spoofing at the network layer, and there is no CA-signed certificate, handshake, or AEAD tag involved in validating a `RecvError` packet at all.

This mirrors the LPS-1 root cause: a privileged/attributed action (in Nebula's case, tearing down another peer's live tunnel and deleting its pending handshake state) is reachable by anyone who can present the right non-secret identifiers, without ever proving ownership of a CA-signed certificate or private key. In LPS-1, the missing check was "only the depositor may unstake"; here, the missing check is "only the actual authenticated remote peer (proven via AEAD-protected traffic) may cause a teardown of this tunnel."

### Impact Explanation
Successful exploitation gives an unauthenticated remote attacker (no CA-signed cert) the ability to invoke `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` on a target's active session, which is a form of remote state poisoning / denial of service: an active, already-negotiated tunnel between two legitimate Nebula nodes can be forcibly and repeatedly torn down by a third party. This can be used to prevent two lighthouse/relay peers from maintaining connectivity, forcing constant re-handshakes, or precisely timed teardown to disrupt traffic — a legitimate protocol-level authentication bypass on the control plane, without any certificate compromise.

### Likelihood Explanation
Exploitability depends on (a) the attacker's ability to learn a victim's `RemoteIndex` (trivial — it's plaintext in every packet header on the wire) and (b) the attacker's ability to send a UDP packet with a spoofed source address matching the victim's current remote endpoint. (b) is feasible for on-path attackers (shared network segment, upstream router, some NAT/ISP configurations) and is a long-standing weak point of UDP-based protocols that don't cryptographically bind control messages. Nebula's own project history acknowledges this risk class (the `listen.send_recv_error` / `listen.accept_recv_error` config knobs exist specifically because "sending these messages can expose the fact that Nebula is running on a host" and to control this tradeoff), but `accept_recv_error` defaults to `"always"`, meaning most deployments are exposed by default: [3](#0-2) 

### Recommendation
Do not honor `RecvError` (or any other unauthenticated control message that mutates peer/tunnel state) based solely on a plaintext index + source-address match. Instead:
- Require the `RecvError` message (or an equivalent signal) to be authenticated — e.g., only accept it if it is delivered as a normal AEAD-protected `Message`/`Control` packet type using the already-established cipher state, so the sender must actually possess the negotiated session keys (i.e., have completed a certificate-authenticated handshake) before it can trigger teardown.
- If an unauthenticated fast-path is kept for performance reasons, treat it only as a *hint* to re-handshake proactively, and never let it unilaterally delete hostinfo/pending state; require the following authenticated packet exchange (e.g., a validated handshake or a valid AEAD test/control packet) to confirm before executing `closeTunnel`/`DeleteHostInfo`.
- Consider defaulting `listen.accept_recv_error` to `"never"` or `"private"` rather than `"always"`, consistent with the documented security tradeoff already known to the team.

### Proof of Concept
1. Two Nebula nodes, `A` and `B`, complete a normal certificate-authenticated handshake and establish an active tunnel.
2. An attacker observes (or otherwise learns) any packet from `A` to `B` and extracts the plaintext header's `RemoteIndex` field (the index `B` uses to identify `A`'s hostinfo) — this requires no decryption since the header is unencrypted.
3. The attacker crafts a bare UDP datagram containing only a `header.H` with `Type = header.RecvError`, `RemoteIndex` set to the value learned in step 2, and spoofs the source `netip.AddrPort` to match `A`'s known/current UDP remote address as seen by `B`.
4. The attacker sends this datagram to `B`'s listening UDP port.
5. `B`'s `readOutsidePackets` routes it directly to `handleRecvError` (bypassing all AEAD/cert verification): [4](#0-3) 
6. `handleRecvError` finds `A`'s hostinfo via `QueryReverseIndex(h.RemoteIndex)`, the spoofed source address matches `hostinfo.GetRemote()`, and `B` calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the legitimate tunnel between `A` and `B` — achieved entirely without possessing any CA-signed certificate or valid session key.

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

**File:** outside.go (L541-574)
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
```

**File:** interface.go (L459-480)
```go
func (f *Interface) reloadAcceptRecvError(c *config.C) {
	if c.InitialLoad() || c.HasChanged("listen.accept_recv_error") {
		stringValue := c.GetString("listen.accept_recv_error", "always")

		switch stringValue {
		case "always":
			f.acceptRecvErrorConfig = recvErrorAlways
		case "never":
			f.acceptRecvErrorConfig = recvErrorNever
		case "private":
			f.acceptRecvErrorConfig = recvErrorPrivate
		default:
			if c.GetBool("listen.accept_recv_error", true) {
				f.acceptRecvErrorConfig = recvErrorAlways
			} else {
				f.acceptRecvErrorConfig = recvErrorNever
			}
		}

		f.l.Info("Loaded accept_recv_error config", "acceptRecvError", f.acceptRecvErrorConfig.String())
	}
}
```
