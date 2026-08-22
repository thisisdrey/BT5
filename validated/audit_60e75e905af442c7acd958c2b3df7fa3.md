### Title
Unauthenticated `RecvError` teardown allows any off-path attacker to force-close established tunnels - (File: outside.go)

### Summary
The external report describes a smart-contract bug class where state-changing functions (`claimRevenueShareDevTeam`, `earningPulls`) have no access-control check on the caller, letting *anyone* invoke them and manipulate the contract's state/funds using attacker-controlled parameters. The reachable analog in nebula is the `header.RecvError` control-message path, which is processed **before** any packet decryption or certificate/session authentication and tears down an existing tunnel based only on a 32-bit index taken from the cleartext packet header plus a UDP source-address comparison that is trivially spoofable.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets in the "Unencrypted packets" branch, alongside `Handshake`, i.e. before the packet has been authenticated against any peer certificate or AEAD key: [1](#0-0) 

`handleRecvError` is the handler invoked for this unauthenticated packet type: [2](#0-1) 

The only "access control" applied is:
1. A local config gate `f.acceptRecvErrorConfig.ShouldRecvError(addr)` (an opt-in/opt-out policy toggle, not a cryptographic check).
2. A comparison of the packet's source `netip.AddrPort` against the hostinfo's currently recorded remote (`hr != addr`).

Neither of these constitutes an access-control check tied to certificate identity: `RemoteIndex` (`h.RemoteIndex`) is transmitted in the clear on every packet header for every message type (see `header.Encode` calls throughout `outside.go`/`inside.go`), so any attacker capable of observing traffic between the two peers — or simply of guessing/enumerating a 32-bit index — already possesses the value needed to reach `QueryReverseIndex`. The remaining "gate" is matching the UDP source `IP:port`, which is unauthenticated at the UDP layer and can be spoofed by an on-path or off-path attacker who can forge the source address of a UDP datagram (a decades-old, well-known weakness of UDP that Nebula's own encrypted/authenticated tunnel is otherwise designed to defend against for every *other* message type).

This mirrors the report's root cause exactly: a state-mutating operation (`closeTunnel` + `handshakeManager.DeleteHostInfo`) is reachable by any network participant with no proof of possessing a CA-signed certificate, gated only by attacker-influenced/spoofable values (`RemoteIndex`, source `AddrPort`) rather than genuine authentication — just as `earningPulls`'s only "access control" was values the caller itself supplied.

### Impact Explanation
An attacker with no valid Nebula certificate who can spoof UDP packets toward a lighthouse or member node (or who is on-path and can observe cleartext headers to learn the target's `RemoteIndex`) can force `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` on a legitimate, fully-established tunnel between two authenticated hosts. This is a remote, unauthenticated denial-of-service / connection-state-poisoning primitive: repeated forged `RecvError` packets can persistently prevent two hosts from maintaining a tunnel (forcing constant re-handshakes), degrading availability and potentially creating a window during re-handshake for further address/roaming manipulation. It does not require the attacker to hold any valid certificate signed by the network's CA.

### Likelihood Explanation
Likelihood is moderate: the attacker needs (a) the `RemoteIndex` (learnable via passive observation of unencrypted headers, since it's sent in the clear on every packet, including handshake and message packets) and (b) the ability to spoof the peer's currently-recorded UDP source address, or to be genuinely on-path/off-path capable of UDP spoofing. Both requirements are commonly achievable by network-level attackers and do not require any cryptographic material, unlike every other packet type nebula processes after the handshake.

### Recommendation
- Do not allow `RecvError` (or any teardown-triggering control message) to mutate hostmap/handshake state based solely on a cleartext index and an unauthenticated source-address match.
- Require some proof tied to the session's cryptographic state (e.g., include a MAC computed with a key derived from the tunnel's negotiated keys, or require an authenticated challenge/response) before honoring a `RecvError`, or at minimum rate-limit and require multiple corroborating signals before tearing down a tunnel.
- Consider making `RecvError` acceptance strictly config-gated to trusted/lighthouse relationships and default-deny for it in security-sensitive deployments, and document that its use is a deliberate availability/security trade-off (partially already noted in `CHANGELOG.md`'s Security section for `send_recv_error`, but the accept-side spoof risk via cleartext index/spoofed source deserves the same explicit warning and hardening).

### Proof of Concept
1. Establish a tunnel between hosts A and B, note that all Nebula packets (handshake and message) carry `RemoteIndex` unencrypted in the header (`header.Encode(...)`).
2. As attacker C (no CA-issued certificate), passively observe or infer A's `RemoteIndex` value used by B (or brute force the 32-bit space).
3. Craft a UDP datagram with `header.RecvError` type/`RemoteIndex` set to A's index, and spoof the source `AddrPort` to match B's currently recorded remote (`hr`) for A — e.g. via a raw socket capable of setting an arbitrary UDP source address, or by being genuinely on-path between A and B.
4. Send this forged packet to A. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, confirms `hr == addr`, and calls `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo(hostinfo)`, immediately terminating A's tunnel state with B with no cryptographic proof of identity from C.

Note: I was unable to fully verify the exact default value/behavior of `acceptRecvErrorConfig`/`ShouldRecvError` (e.g., whether it is enabled by default) since `interface.go`'s implementation of `ShouldRecvError` was not retrievable within the available search results before the iteration limit; if it defaults to disabled or restricts to previously-seen addresses only, that would reduce (but not eliminate, given UDP source spoofing) the practical likelihood of this issue.

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
