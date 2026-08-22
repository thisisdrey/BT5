### Title
Unauthenticated `RecvError` packets let an attacker without a valid certificate force-teardown a victim's established tunnel using an attacker-controlled index/address parameter that is never cryptographically bound to the sender - (File: outside.go)

### Summary
The Aave Lens report's root cause is that a privileged state-changing operation (`processFollow`) trusts a caller-supplied identity parameter (`follower`) instead of verifying that the actual authenticated caller (`msg.sender`) is the party being acted upon. The nebula analog is `Interface.handleRecvError` in [1](#0-0) : it accepts a plaintext, unauthenticated `RecvError` packet type and uses two attacker-influenced values — the wire `RemoteIndex` field and the UDP source address — to locate and then tear down another host's live tunnel, without any cryptographic proof that the sender is the actual remote peer of that tunnel.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets to `f.handleRecvError` before any certificate/handshake state is required [2](#0-1) . This packet type is deliberately unauthenticated: it carries only a plaintext header with a `RemoteIndex` field and is processed for hosts that have never presented a certificate.

```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		...
		return
	}
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
``` [1](#0-0) 

The only "authentication" performed is comparing the spoofable UDP source address (`addr`) against the hostinfo's last-known remote address (`hr`), and this check is entirely skipped when `hr.IsValid()` is false. The `RemoteIndex` used to look up the hostinfo is the plaintext index that appears in the unencrypted header of every packet traded between two peers [3](#0-2) , so any observer of the traffic (not just a certificate holder) can learn it. Combined with UDP source-address spoofing, an attacker with no CA-signed certificate can trigger `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` for a tunnel belonging to two legitimate, fully-authenticated peers — exactly the same class of bug as the Lens report: a privileged/stateful action is driven by an attacker-supplied parameter (`RemoteIndex`/source address) rather than a value bound to a verified identity (a valid Noise/certificate handshake).

This differs from the legitimate use of `RecvError`, which is meant to let a peer that has *lost* a tunnel (index unknown, no hostinfo) tell the other side to fast-reconnect; here the same message type is repurposed by an unauthenticated third party to sabotage a tunnel that is still alive and well-known to both real peers.

### Impact Explanation
An attacker with no valid certificate and no participation in the Noise handshake can force-terminate an established, authenticated tunnel between two legitimate nebula nodes, causing denial of service and remote state poisoning (deleting hostmap/pending-handshake entries) on the target node. This matches the "remote state poisoning" / DoS acceptance criteria and mirrors the report's core lesson: a mutable/unauthenticated code path acts on a security-relevant identity/index that was supplied by the packet, not verified against the actual authenticated session.

### Likelihood Explanation
Exploitation requires the attacker to know the victim's `RemoteIndex` (learnable via network observation of the plaintext header, or brute force since it's a 32-bit index) and to spoof the victim's expected UDP source address (feasible for many off-path/on-path attackers over UDP, or trivially satisfied whenever `hr.IsValid()` is false, e.g. immediately after a handshake or roam). No certificate, CA trust, or successful handshake is required to reach this code path, satisfying the "no CA-signed certificate" reachability constraint.

### Recommendation
Do not allow a plaintext, unauthenticated message to tear down or mutate the state of an established, cryptographically-verified tunnel. Options:
1. Require `RecvError` handling to also validate a value that only the genuine remote peer could produce (e.g., require it be sent from an address that matches an established conntrack/UDP session and treat address mismatch as fatal rather than skipping the check when `hr` is invalid).
2. Rate-limit/log-and-ignore `RecvError` for hostinfos with an active, recently-verified `ConnectionState` rather than immediately closing the tunnel.
3. Consider authenticating `RecvError` (e.g., by having the sender include a MAC keyed by session state) so it cannot be forged by a party that never completed the handshake.

### Proof of Concept
1. Two nebula peers, A and B, complete a handshake and establish a tunnel; A's `RemoteIndex` (for B's copy of the hostinfo) is visible in the plaintext header of every packet A and B exchange (`header.H`).
2. An attacker C, holding no CA-signed certificate, observes/guesses this `RemoteIndex` and crafts a `header.RecvError` packet: `header.Encode(buf, header.Version, header.RecvError, 0, remoteIndex, 0)`.
3. C spoofs the UDP source address to match B's known remote address for that tunnel (or sends before B has re-validated `hr`, when `hr.IsValid()` is false) and sends the forged packet to A.
4. `readOutsidePackets` routes the packet type `header.RecvError` straight to `handleRecvError` with no certificate/handshake check [2](#0-1) .
5. `handleRecvError` finds A's hostinfo for B via `QueryReverseIndex`, the address check passes or is skipped, and A calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying the legitimate tunnel state without any participation from B [4](#0-3) .

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
