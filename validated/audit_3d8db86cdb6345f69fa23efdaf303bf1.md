## Analog Vulnerability Analysis

The external report concerns unconditional trust granted to an unverified party (a hardcoded operator address) with no cryptographic proof of authorization. The strongest reachable analog in this codebase is the unauthenticated `RecvError` control-message handler, which tears down an active tunnel based solely on unauthenticated header fields (a guessable 32-bit index plus a spoofable source `UDP` address) with no certificate, HMAC, or encryption backing the packet.

### Title
Unauthenticated `RecvError` packet allows remote tunnel state poisoning without a CA-signed certificate - (File: `outside.go`)

### Summary
`RecvError` packets are processed before any certificate or connection-state check exists (they are handled directly in `readOutsidePackets` before decryption, alongside `Handshake` packets). The handler in `handleRecvError` [1](#0-0)  trusts the message purely on the basis of an unauthenticated 32-bit index (`h.RemoteIndex`) and an IP/port match against the currently-stored remote address, then tears down the tunnel.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets straight to `f.handleRecvError` without any decryption, MAC, or certificate verification step — this is a deliberate "unencrypted packet" path alongside handshake packets: [2](#0-1) 

`handleRecvError` then:
1. Looks up the hostinfo by the attacker-controlled `RemoteIndex` field.
2. Compares the packet's source address to the currently known remote address (`hr`), and if they match, unconditionally tears down the tunnel: [3](#0-2) 

There is no signature, no MAC, and no certificate check anywhere in this path — the only gate is `f.acceptRecvErrorConfig.ShouldRecvError(addr)`, which by default is `recvErrorAlways` (accept unconditionally) [4](#0-3) . This mirrors the report's bug class: an entity is granted the ability to unilaterally act on another party's state (destroy an active session) based on an implicitly/automatically trusted signal rather than a verified, revocable credential — the "hardcoded, unverified trust" pattern from the OpenSea-operator report, translated to control-plane trust instead of contract-approval trust.

### Impact Explanation
An attacker with no CA-signed certificate who can (a) observe or brute-force a target's 32-bit local handshake index and (b) spoof the UDP source address of the legitimate peer can force `closeTunnel` and `DeleteHostInfo` on the victim, causing a denial of service and forcing costly re-handshakes — remote state poisoning of the hostmap/tunnel lifecycle, achieved with a single 4-byte-header UDP packet and zero cryptographic material.

### Likelihood Explanation
UDP source-address spoofing is trivially available to a network-path attacker (no socket-level source verification exists at the UDP layer), and `RemoteIndex` values, while 32-bit, are observable by any attacker who can see traffic on the wire (e.g., a relay, an on-path router, or anyone who previously communicated with either party) since they are sent in cleartext in every packet header. Combined with `recv_error` being accepted "always" by default, this makes exploitation practical for a passive on-path or previously-communicating attacker without needing a valid Nebula certificate.

### Recommendation
- Require the `RecvError` handler to also validate that the packet was cryptographically produced by the claimed peer (e.g., authenticate it under the tunnel's established symmetric keys, similar to how relay frames are handled via `VerifyRelay`) rather than relying only on `RemoteIndex` + source-IP-address matching.
- Alternatively, retire unauthenticated `RecvError` teardown entirely in favor of an authenticated control message, or require multiple corroborating signals (e.g., a nonce echoed from a prior valid handshake) before tearing down state.
- At minimum, default `listen.accept_recv_error` to `private` or `never` rather than `always`, reducing the unauthenticated attack surface reachable from the public internet.

### Proof of Concept
1. Attacker observes (or is on-path for) traffic between victim A and victim B, learning A's `localIndexId` (sent in cleartext in every packet's `RemoteIndex` field to B) and B's UDP address.
2. Attacker crafts a `RecvError` packet (`header.Encode` with `header.RecvError` type and the observed index) and spoofs the source address to match B's UDP address, sending it to A.
3. `handleRecvError` on A finds the hostinfo via `QueryReverseIndex`, sees `hr == addr` (the spoofed address matches the trusted remote), and calls `closeTunnel` + `DeleteHostInfo`, tearing down A's tunnel to B — achieved with no certificate, no handshake, and no possession of any cryptographic key material.

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
