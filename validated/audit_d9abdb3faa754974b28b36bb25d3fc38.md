### Title
Unauthenticated `RecvError` packet can tear down a tunnel without any source verification when no direct remote is recorded - ([File: outside.go])

### Summary
`RecvError` is a plaintext, unauthenticated Nebula message type. `readOutsidePackets` dispatches it straight to `f.handleRecvError` before any certificate, hostinfo, or connection-state check is performed [1](#0-0) . `handleRecvError` looks up the target tunnel purely by the attacker-supplied `h.RemoteIndex` and only refuses to act if the hostinfo already has a *valid* recorded remote address that mismatches the packet's source; when no such remote address is recorded, the packet is accepted with no verification at all and the tunnel is torn down [2](#0-1) .

### Finding Description
The root cause mirrors the Ammplify bug: a security-critical action (in Ammplify, minting/transferring funds via a caller-supplied `pool` address; here, tearing down an authenticated tunnel) is gated on a value supplied entirely by the untrusted caller, with no independent verification that the value legitimately identifies the entity it claims to represent.

`handleRecvError` resolves the target `HostInfo` via `f.hostMap.QueryReverseIndex(h.RemoteIndex)` [3](#0-2) . `RemoteIndex` is a 32-bit value carried in cleartext in every Nebula packet header, so it is not a secret and is observable/derivable by any network-path or relay-adjacent observer. The only anti-spoofing check is:
```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    ...
    return
}
f.closeTunnel(hostinfo)
``` [4](#0-3) 
This check is conditioned on `hr.IsValid()`. If the hostinfo has no recorded direct remote (e.g., a relay-only tunnel that has never sent a directly-addressed packet, or one where `SetRemote` has not yet populated the atomic pointer), the address-equality guard never runs, and the destructive `closeTunnel` + `handshakeManager.DeleteHostInfo` path executes solely because the attacker supplied a matching `RemoteIndex`, with zero authentication of the sender. This is reachable by any attacker on the network path, without completing a handshake and without holding a CA-signed certificate at all, exactly like the Ammplify report's unauthenticated `pool` parameter that was trusted without checking it was actually issued by the canonical factory.

### Impact Explanation
An unauthenticated attacker who observes or derives the `RemoteIndex` of a tunnel (readily available from any packet on that tunnel, including relayed traffic) can forge a single-byte-header `RecvError` UDP packet and forcibly tear down that tunnel whenever the victim has no recorded direct remote address for it — this is a remote state-poisoning / denial-of-service primitive against established Nebula tunnels, particularly relay-dependent ones, achievable without any certificate or successful handshake.

### Likelihood Explanation
`RecvError` handling sits ahead of every authentication and hostinfo/connection-state check in the inbound packet pipeline [1](#0-0) , so the attacker needs no valid certificate, no completed handshake, and no correct source address in the common case where `hr.IsValid()` is false. The only prerequisite is knowledge of a live `RemoteIndex`, which is transmitted in plaintext on every packet of the tunnel.

### Recommendation
Do not act on `RecvError` (or any other unauthenticated control message) as an unconditional signal to tear down a tunnel. At minimum, require the source address check unconditionally (fail closed instead of skipping the check when `hr` is invalid), and/or require some additional proof of legitimacy (e.g., only accept `RecvError` from an address already present in the host's remote candidate list, or rate-limit / cross-check against recent egress) before calling `closeTunnel`.

### Proof of Concept
1. Observe or infer a victim's live tunnel `RemoteIndex` (visible in cleartext Nebula headers, including relayed frames).
2. From any address, without possessing a Nebula certificate or completing any handshake, send a `header.RecvError` packet with that `RemoteIndex`.
3. If the victim's `HostInfo` for that tunnel has no `hr.IsValid()` remote recorded (e.g., relay-only path), `handleRecvError` skips the spoofing check and calls `f.closeTunnel(hostinfo)` / `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel purely on the attacker's say-so [5](#0-4) .

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
