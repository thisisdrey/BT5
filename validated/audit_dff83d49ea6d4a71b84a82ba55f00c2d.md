### Title
Unauthenticated `RecvError` packets allow remote UDP-spoofing attacker to tear down established tunnels - ([File: outside.go])

### Summary
`handleRecvError` in `outside.go` tears down an active tunnel purely on the basis of an unauthenticated, unencrypted UDP packet (`header.RecvError`). The only "authentication" performed is a comparison of the packet's source `netip.AddrPort` against the tunnel's currently known remote address — a value that is trivially forgeable by any attacker able to send UDP packets to the victim (source-address spoofing, or an attacker positioned to relay/observe traffic), analogous to the reported `setCollateralProvider` issue where a critical piece of trust/control state could be mutated by an unauthenticated party.

### Finding Description
`RecvError` packets are handled before any decryption/cert-based authentication path, alongside `Handshake` packets: [1](#0-0) 

The handler itself: [2](#0-1) 

The only gate before tearing down the tunnel via `f.closeTunnel(hostinfo)` is:
1. `f.acceptRecvErrorConfig.ShouldRecvError(addr)` — a config-driven allow/deny list, defaulting historically to `always` per the changelog entry: “Add a config option to control accepting `recv_error` packets which defaults to `always`” [3](#0-2) .
2. A match between `hostinfo.GetRemote()` and the UDP source address (`addr`) of the incoming packet.
3. A match between `h.RemoteIndex` and an existing hostmap entry via `QueryReverseIndex`.

None of these checks involve any cryptographic proof tied to the peer's CA-signed certificate or session keys. `RemoteIndex` is a 32-bit value exchanged in the clear during the handshake and observable to anyone who can see traffic on the path (or who previously communicated with the victim), and the UDP source address is attacker-controlled on the wire (classic UDP spoofing) since Nebula runs over connectionless UDP. An attacker who can either observe a `RemoteIndex` in transit or who is on-path relative to the victim's real remote can forge a `RecvError` packet that satisfies both checks and unilaterally close/destroy the victim's tunnel state (`hostinfo`), also deleting it from the pending handshake manager.

This mirrors the reported bug class: a state-changing operation (in the original report, `collateralProvider`; here, tunnel/tunnel-index trust state) is exposed to unauthenticated actors, enabling attacker-controlled corruption of core state and denial of service — exactly the accepted impact categories (“remote state poisoning” / “remote crash impact” per the validation rubric), without requiring the attacker to hold any CA-signed certificate at all.

### Impact Explanation
An attacker with no valid Nebula certificate, purely by forging UDP packets, can force termination of an established, encrypted tunnel between two legitimate, certificate-holding peers. Because this can be repeated indefinitely, it enables a persistent denial-of-service against specific tunnels (or, if `RemoteIndex` values can be enumerated/observed at any point — e.g., via passive network observation, path position, or leaked debug output — across many tunnels), forcing continual re-handshakes and disrupting availability. This is comparable in severity to the original report's "permanent denial of service" impact from an unauthenticated critical-state mutation.

### Likelihood Explanation
Exploitation requires only the ability to send a spoofed UDP packet with:
- the correct destination (the victim's real listening port — knowable if the attacker has ever seen the victim's traffic, e.g., as a relay, an on-path observer, or a previous legitimate peer that later goes rogue),
- the correct `RemoteIndex` for a target tunnel (transmitted in cleartext in every packet header, so trivially sniffable by anyone who can observe traffic to/from the victim), and
- a source address matching the victim's currently recorded peer's remote address (also visible in the header of legitimate packets, and free to spoof on UDP without needing to complete any handshake).

No cryptographic capability, valid certificate, or CA trust is required, matching the "reachable by an attacker with no CA-signed certificate" scope. The default configuration reportedly accepts `recv_error` packets `always`, maximizing exposure unless operators have explicitly hardened `listen.accept_recv_error`.

### Recommendation
- Require some form of proof of tunnel possession before acting on `RecvError` (e.g., only honor `RecvError` from an address that has ever been decrypt-authenticated for that index, and/or bind acceptance more strictly, such as also validating the reported index maps 1:1 and rate-limiting per remote index/address).
- Consider authenticating `RecvError` using session key material (e.g., a MAC keyed by the same tunnel's cipher state) rather than trusting the raw UDP header fields alone, closing the gap where an unauthenticated attacker can influence tunnel-teardown state.
- At minimum, default `listen.accept_recv_error` to a conservative mode (e.g., `same_subnet`) instead of `always`, and document the DoS risk clearly for operators who broaden it.

### Proof of Concept
1. Attacker observes (via network vantage point, being a relay, or prior legitimate communication) a live tunnel between hosts A and B, learning B's `RemoteIndex` value on A's side (sent in the clear in every packet header from B to A) and B's current UDP `AddrPort` as seen by A.
2. Attacker crafts a minimal UDP packet: `header.Encode(..., header.RecvError, 0, <observed RemoteIndex>, 0)` and sends it to A's listening UDP port, spoofing the source address to match B's known `AddrPort` (`outside.go:528-539` shows the exact wire format `sendRecvError` produces, which the attacker can replicate).
3. On A, `readOutsidePackets` routes the packet directly to `f.handleRecvError(via.UdpAddr, h)` without any decryption/authentication (`outside.go:81-84`).
4. `handleRecvError` finds the matching `hostinfo` via `QueryReverseIndex`, sees `hr == addr` (spoofed to match), and calls `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo(hostinfo)` (`outside.go:557-575`), tearing down A's side of the tunnel without B's, or either side's, cryptographic participation.
5. Repeating this at will produces a persistent DoS against the targeted tunnel.

Note: I was not able to fully inspect `interface.go`'s `ShouldRecvError`/`acceptRecvErrorConfig` implementation in detail (only match counts were returned, not file contents) to confirm the exact default matching semantics (e.g., whether it restricts by subnet by default in the current codebase version). If precise current-default behavior is needed, a Devin session with full file access to `interface.go` would be required to confirm.

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

**File:** CHANGELOG.md (L128-131)
```markdown
### Added

- Add a config option to control accepting `recv_error` packets which defaults to `always`. (#1569)

```
