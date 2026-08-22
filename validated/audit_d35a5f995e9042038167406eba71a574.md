`handleRecvError` in `outside.go` is a plausible analog. It processes an unauthenticated, unencrypted `RecvError` packet type (dispatched before any decryption in `readOutsidePackets`) and, based purely on `h.RemoteIndex` from that unauthenticated header plus a UDP source-address match, tears down an established tunnel via `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`. [1](#0-0) [2](#0-1) 

### Title
Unauthenticated `RecvError` packet allows remote tunnel teardown via spoofed source address - (File: outside.go)

### Summary
`RecvError` packets are handled entirely outside the AEAD-authenticated data path: `readOutsidePackets` dispatches `header.RecvError` to `f.handleRecvError` before any decryption or certificate check occurs. `handleRecvError` looks up the target `HostInfo` purely by the attacker-supplied `h.RemoteIndex` field and only checks that the packet's *source UDP address* equals the value currently stored in `hostinfo.GetRemote()`. Neither of these values is cryptographically bound to the sender: an attacker with no CA-signed certificate can guess/observe a live tunnel's remote index and spoof the victim's UDP source address to force the responder to tear the tunnel down.

### Finding Description
In `readOutsidePackets`, `header.RecvError` is handled in the "Unencrypted packets" switch, before any lookup of `hostinfo.ConnectionState` or AEAD verification: [1](#0-0) 

`handleRecvError` then:
1. Looks up the `HostInfo` from `f.hostMap.QueryReverseIndex(h.RemoteIndex)` — `h.RemoteIndex` is an unauthenticated header field parsed straight off the wire.
2. Compares `addr` (the packet's UDP source address, which is trivially spoofable on UDP) against `hostinfo.GetRemote()`.
3. If they match, it calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the tunnel. [2](#0-1) 

This mirrors the structure of the reported Vader bug class: a caller-supplied identifier (`h.RemoteIndex`, analogous to the `from` parameter) is trusted to select which victim state to act on, and the only "authentication" is a value (the source IP/port) that is not cryptographically tied to the actual sender — exactly like trusting an attacker-controlled `to`/`from` address instead of `msg.sender`. Because UDP allows arbitrary source-address spoofing (especially over networks/paths without egress filtering), an attacker who can guess or observe `h.RemoteIndex` for a live tunnel (indices are only 32-bit values transmitted in cleartext on every packet of that tunnel) can spoof the legitimate peer's UDP address and inject a bare `RecvError` packet to force tunnel teardown on the responder.

### Impact Explanation
An off-path/unauthenticated attacker (no valid Nebula certificate is needed since this code path runs before any cert/handshake state is consulted) can remotely terminate arbitrary established Nebula tunnels by spoofing the UDP source address of a legitimate peer and supplying that peer's remote index. This is a remote denial-of-service against overlay connectivity: repeated forged `RecvError` packets can continuously tear down tunnels, disrupting all traffic between the victim and its peer, and forcing costly re-handshakes. It does not by itself allow decryption or forgery of traffic, but it is a remote-state-poisoning / connection-teardown primitive reachable without any credentials.

### Likelihood Explanation
Exploitability depends on the attacker's ability to spoof the victim's UDP source address (feasible on many networks lacking BCP38/egress filtering, and often trivial on the same LAN/broadcast domain) and to learn/guess a live `RemoteIndex`, which is a 32-bit value sent unencrypted in every packet header of an ongoing tunnel and thus observable to any on-path or promiscuous listener, or via traffic analysis. `sendRecvErrorConfig`/`acceptRecvErrorConfig` (`ShouldRecvError`) gate this feature and may be disabled by default in some deployments, which somewhat limits universal exploitability, but where enabled the check performs no cryptographic verification of sender identity.

### Recommendation
Do not act on `RecvError` solely based on the cleartext `RemoteIndex` and spoofable UDP source address. Require the `RecvError` handling to be rate-limited per (index, address) pair, and/or require some proof of tunnel knowledge (e.g., only accept it if the address also matches the *most recently learned* punch/lighthouse-verified address, and treat repeated close events with backoff/quorum), or move recv-error acknowledgement into the authenticated channel where feasible, so an attacker cannot single-packet-teardown a tunnel purely from spoofed metadata.

### Proof of Concept
1. Establish a Nebula tunnel between hosts A and B; observe on the wire (or infer) B's `RemoteIndex` value that A uses when sending to B (it is present in cleartext in the header of every UDP packet A sends).
2. From attacker-controlled infrastructure with the ability to spoof UDP source addresses as A's UDP endpoint (`hostinfo.GetRemote()` on B), or from a vantage point on path to B, craft a bare `RecvError` header packet: `header.Encode(..., header.RecvError, 0, <B's index for A>, 0)` with spoofed source `A_udp_addr:A_udp_port`.
3. Send this packet to B's listening UDP port.
4. If `f.acceptRecvErrorConfig.ShouldRecvError(addr)` allows it (configuration-dependent) and the spoofed address matches `hostinfo.GetRemote()`, B calls `closeTunnel`/`DeleteHostInfo`, terminating the tunnel to A without any cryptographic proof that A sent this packet.

Note: I was unable to fully verify default enablement of `acceptRecvErrorConfig` (its `ShouldRecvError` gating logic) within this session's index coverage; a background Devin session with full repository access would be needed to confirm default config values and any additional rate-limiting already present in `sendRecvErrorConfig`/`acceptRecvErrorConfig` before treating this as conclusively exploitable in default deployments.

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
