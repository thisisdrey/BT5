### Title
Unauthenticated `RecvError` packets allow tunnel-teardown griefing against other peers' sessions - (File: outside.go)

### Summary
Nebula's handshake/message header (including `RemoteIndex`) is transmitted in cleartext, and the `header.RecvError` control message is processed and acted upon (tearing down a tunnel) with only a weak, spoofable source-address check. This mirrors the Sherlock H-4 pattern: a party with no special privilege (no valid CA-issued certificate, no established session) can invalidate/destroy another party's already-established state by crafting a message that reuses public, observable identifiers (there: nonce; here: `RemoteIndex`).

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets to `f.handleRecvError` before any decryption/authentication is required [1](#0-0) . `handleRecvError` looks up the hostinfo purely by the cleartext `h.RemoteIndex` and only rejects the message if the hostinfo's currently known remote endpoint (`hr`) is valid and differs from the packet's source address; if that address matches (or if `hr` is not yet valid, e.g. mid-roam or via a relay), the tunnel is torn down and the pending handshake state is deleted: [2](#0-1) 

Because message and handshake headers are sent unencrypted on the wire, `RemoteIndex` values for arbitrary in-progress tunnels between two other peers are trivially observable by anyone who can see the UDP traffic (no certificate or handshake participation required) [3](#0-2) . UDP has no source-address authentication, so the `hr != addr` check in `handleRecvError` can be satisfied by simply spoofing the source IP:port to match the victim's known remote — the same "observe public identifier, then front-run/forge a control message using it" pattern as the original `checkOrder()`/nonce bug, except here the invalidated resource is a live tunnel/hostinfo instead of a presigned order nonce.

This is structurally the same class of bug as H-4: a state-marking/invalidation primitive (`_useNonce()` there, `closeTunnel` + `handshakeManager.DeleteHostInfo` here) is reachable through a code path that lacks the authentication context of the primary flow, letting an unrelated third party invalidate another pair's already-established/pending state using only externally-observable identifiers.

### Impact Explanation
An attacker who can observe traffic between two Nebula peers (e.g., on-path or by receiving broadcast/multicast on a shared segment, or simply by being another lighthouse/underlay-reachable host) can extract the cleartext `RemoteIndex` of that tunnel and send a spoofed UDP `RecvError` packet claiming to originate from the peer's current remote address. If accepted, this forces `closeTunnel` (tearing down the active tunnel) and deletes the pending hostinfo entry, forcing constant re-handshaking — a remote, unauthenticated denial-of-service/griefing vector against tunnels the attacker is not a party to, without needing a CA-signed certificate.

### Likelihood Explanation
Requires: (1) ability to observe the cleartext header of the target tunnel's traffic to learn `RemoteIndex`, and (2) ability to spoof the UDP source address to match the victim's currently known remote endpoint. Both conditions are plausible for an on-path or same-broadcast-domain attacker without any certificate, similar in spirit to the mempool-observation + front-run described in H-4. The severity is bounded by the fact that the tunnel is simply rebuilt via re-handshake, but repeated injection causes persistent disruption.

### Recommendation
- Require `RecvError` handling to only affect a hostinfo whose remote endpoint is unset/unverified in a way that cannot be forced by an unauthenticated third party, or better, require `RecvError` to be authenticated (e.g., protected by the tunnel's AEAD key / delivered under an established `ConnectionState`) rather than accepted purely based on `RemoteIndex` + a comparably weak source-IP match.
- Rate-limit and require stronger corroboration (e.g., matching a still-pending outbound send, or short validity window tied to a recent handshake) before tearing down state on receipt of an unauthenticated control message.
- Consider not exposing `RemoteIndex` in cleartext handshake/message headers where avoidable, or treat it as a low-trust hint only.

### Proof of Concept
1. Attacker passively observes UDP traffic between victim tunnel peers A and B (headers are unencrypted, see `header.Encode` usage in `handshake_manager.go`), recording B's `localIndexId`/`RemoteIndex` value used on the wire.
2. Attacker crafts a UDP packet with `header.RecvError` type and `RemoteIndex` set to the observed index, and spoofs the source address to match A's currently known remote (`hostinfo.GetRemote()`), as read in `outside.go` lines 557–570.
3. On receipt, `handleRecvError` finds the hostinfo via `f.hostMap.QueryReverseIndex(h.RemoteIndex)`, the `hr != addr` check passes because the source is spoofed to match, and `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` execute, terminating the legitimate tunnel between A and B without either party having sent the error.

Note: I was not able to fully trace the exact conditions under which `hr.IsValid()` is false in production flows (e.g. during roaming or relay-only sessions) within the available index; a full verification of all guard conditions around `sendRecvErrorConfig`/`acceptRecvErrorConfig` rate limiting in `interface.go` would benefit from a deeper read of that file, which the current search only partially covered.

### Citations

**File:** outside.go (L81-84)
```go
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

**File:** handshake_manager.go (L151-185)
```go
func (hm *HandshakeManager) HandleIncoming(via ViaSender, packet []byte, h *header.H) {
	// Gate on known handshake subtypes. Unknown subtypes (or future ones we
	// don't yet support) are dropped here rather than silently routed through
	// the IX path. Add a case when introducing a new pattern.
	switch h.Subtype {
	case header.HandshakeIXPSK0:
		// supported
	default:
		hm.l.Debug("dropping handshake with unsupported subtype",
			"from", via, "subtype", h.Subtype)
		return
	}

	// First remote allow list check before we know the vpnIp
	if !via.IsRelayed {
		if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
			hm.l.Debug("lighthouse.remote_allow_list denied incoming handshake", "from", via)
			return
		}
	}

	// First message of a new handshake. The wire format requires RemoteIndex
	// to be zero here (the initiator has no responder index to fill in yet),
	// and generateIndex never allocates 0, so any non-zero RemoteIndex on a
	// stage-1 packet is malformed or someone probing for an index collision.
	// Drop without paying the cost of running noise on a pending Machine.
	if h.MessageCounter == 1 {
		if h.RemoteIndex != 0 {
			hm.l.Debug("dropping stage-1 handshake with non-zero RemoteIndex",
				"from", via, "remoteIndex", h.RemoteIndex)
			return
		}
		hm.beginHandshake(via, packet, h)
		return
	}
```
