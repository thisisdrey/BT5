### Title
Unauthenticated `RecvError` packets let a remote attacker tear down an established tunnel without any cryptographic proof - (File: outside.go)

### Summary
The external report flags `voluntaryExit` being callable by anyone (public) as dangerous because it lets an untrusted caller trigger a state-changing action / event that should only ever be reachable through an internal, already-verified code path. The reachable Nebula analog is `handleRecvError` in `outside.go`, which is dispatched straight from `readOutsidePackets` for any packet with `header.RecvError` type, *before* any AEAD decryption or certificate/handshake authentication is performed. Sending this packet type causes `f.closeTunnel(hostinfo)` to run, tearing down an active, fully-authenticated tunnel — a state-changing "event" triggered by an unauthenticated actor.

### Finding Description
`readOutsidePackets` demultiplexes on the wire header type before any cryptographic check is applied to most packet types: [1](#0-0) 

`header.RecvError` is handled immediately, with only the raw source `via.UdpAddr` and the plaintext `RemoteIndex` field from the (unauthenticated, unencrypted) header as inputs: [2](#0-1) 

The only gate before tearing down the tunnel is a comparison of the packet's claimed sender address against the hostinfo's currently known remote address: [3](#0-2) 

There is no verification that the sender actually possesses the session key, no certificate check, and no proof that the sender is the genuine remote peer — only an index lookup and an address string compare, both of which are attacker-influenceable:
- `RemoteIndex` is a 32-bit value that is sent in plaintext in the header of every packet exchanged over the tunnel, so any passive observer on the path (or anyone who can see even a single packet, e.g. via NAT traversal/hole punching broadcasts) learns it trivially.
- `addr` is the UDP source address taken from the packet as received; it is not cryptographically bound to the sender, so it can be spoofed at the network layer, and if the target's remote is not yet resolved (`hr.IsValid()` false) the check is skipped entirely.

This mirrors the audited bug class precisely: a state-mutating operation (`closeTunnel`, i.e. the network analog of `voluntaryExit`) is reachable from a "public" (unauthenticated) entry point instead of being confined to callers that have already passed the relevant verification (handshake / AEAD decryption), so an untrusted party can generate an unintended "event" (tunnel teardown) that legitimate, authenticated logic should have gated.

### Impact Explanation
An attacker who can observe or spoof a single UDP packet's index/address pair for an established Nebula tunnel can force that tunnel to be deleted from the hostmap and pending handshake map (`f.closeTunnel` + `f.handshakeManager.DeleteHostInfo`) without ever completing a handshake or holding any valid certificate. This is a remote, unauthenticated denial-of-service / forced-reconnect primitive against arbitrary established tunnels — a form of remote state poisoning of the mesh's connection state, achievable purely from the "no CA-signed certificate" attacker position that this analysis scope allows.

### Likelihood Explanation
The default configuration (`listen.accept_recv_error: always`) accepts these packets from anyone; the `recvErrorPrivate`/`recvErrorNever` settings are opt-in mitigations, so a stock deployment is exposed. `RemoteIndex` is transmitted unencrypted on every packet on the wire, making it observable to any on-path or off-path attacker capable of a single sniff or of guessing across the 32-bit space combined with address spoofing. The maintainers themselves have previously acknowledged the sensitivity of this path (adding `listen.send_recv_error`/`listen.accept_recv_error` toggles), which corroborates that the exposure is real, even though it ships enabled by default.

### Recommendation
Do not let an unauthenticated `RecvError` packet, gated only by an address string match, directly invoke `closeTunnel`. At minimum:
- Require the accompanying `MessageCounter`/index to correspond to a session state that additionally proves knowledge of the AEAD key (e.g., only honor `RecvError` when it can be validated against recent state such as connection-manager activity windows, or require it to be authenticated similarly to other in-tunnel control messages).
- Change the default `listen.accept_recv_error` posture to a more restrictive default (e.g., `private`) rather than `always`, so plain internet-reachable nodes are not trivially torn down by a spoofed/observed index.
- Consider rate limiting/backoff on repeated `RecvError`-triggered teardowns per remote index to blunt reconnect-storm abuse even when the check passes.

### Proof of Concept
1. Establish a tunnel between `me` and `them` (as in the existing `TestCloseTunnelAuthenticated` pattern in `e2e/tunnels_test.go`).
2. Observe (or otherwise learn) the `RemoteIndex` used by one side for the tunnel — it is sent unencrypted in every packet header on the wire.
3. Craft a bare `header.H{Type: header.RecvError, RemoteIndex: <observed index>}` packet (no encryption, no valid cert) and send/spoof it from an address matching the peer's currently known remote (or before the remote is resolved, from any address).
4. `readOutsidePackets` routes it to `handleRecvError`, which finds the hostinfo via `QueryReverseIndex`, passes the address check, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` — tearing down the legitimate tunnel with no cryptographic proof of authorization, exactly as `TestCloseTunnelAuthenticated`'s bogus-`CloseTunnel` test demonstrates is normally rejected for the authenticated `CloseTunnel` path but is not rejected here for `RecvError`.

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

**File:** outside.go (L541-561)
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
```

**File:** outside.go (L563-575)
```go
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
