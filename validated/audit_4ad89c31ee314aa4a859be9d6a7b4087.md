### Title
Unauthenticated `RecvError` packets allow remote attacker to force-close established tunnels via UDP source-address spoofing - (File: outside.go)

### Summary
`RecvError` is one of the plaintext/unauthenticated message types that Nebula processes *before* any certificate-backed session state is consulted [1](#0-0) . `handleRecvError` accepts this unauthenticated packet and, if the source `netip.AddrPort` happens to match the hostinfo's currently recorded remote address, it immediately tears down the tunnel and deletes the pending handshake state [2](#0-1) . Because UDP source addresses are trivially spoofable and the check performed is a plain address-equality comparison rather than any cryptographic proof of possession, an attacker who holds no CA-signed certificate at all can spoof this single unauthenticated packet type to poison/tear down a tunnel between two legitimately certificated peers.

### Finding Description
Inbound packets in `readOutsidePackets` are dispatched by `h.Type` immediately after header parsing and before hostinfo/certificate lookup for two types: `header.Handshake` and `header.RecvError` [3](#0-2) . `RecvError` packets carry no AEAD tag, no Noise session binding, and no certificate — they are 12-byte plaintext headers encoding only a `RemoteIndex` [4](#0-3) .

`handleRecvError` performs the following checks before tearing down a tunnel:
1. A config-driven rate/allow gate `f.acceptRecvErrorConfig.ShouldRecvError(addr)` [5](#0-4) .
2. A hostmap lookup by `RemoteIndex` via `QueryReverseIndex` [6](#0-5) .
3. A comparison of the packet's UDP source address against the hostinfo's currently known remote address [7](#0-6) .

None of these steps require the sender to possess a Nebula certificate, a valid Noise session, or any cryptographic secret. `RemoteIndex` is a 32-bit value that traverses the wire in the clear on every data packet a victim sends, so an on-path or off-path attacker who can observe or guess it (and spoof the victim peer's UDP source address/port) can trigger `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` on the receiving node, tearing down an established, mutually-authenticated tunnel without ever presenting a certificate signed by the network's CA [8](#0-7) .

This is the direct networking analog of the reported bug class: a security-relevant operation (`closeTunnel`/state teardown) is reachable through a code path (`RecvError` handling) that lacks the authorization check (cryptographic authentication) that the "intended" path (Noise-encrypted, cert-verified data plane) enforces — exactly the "bypass the restricted path via an unguarded alternate entry point" pattern described in the external report, mapped onto Nebula's pre-certificate/unauthenticated packet-type surface.

The project's own `CHANGELOG.md` documents prior incidents in this exact area — "Valid recv_error packets were incorrectly marked as spoofing and ignored" (#482) and "Disable sending recv_error messages when a packet is received outside the allowable counter window" (#1459) [9](#0-8)  — confirming this is a recognized, historically fragile trust boundary, though the underlying address-equality check (rather than cryptographic authentication) remains.

### Impact Explanation
An attacker with no CA-signed certificate and no established tunnel to the victim can force termination of a legitimate, already-authenticated tunnel between two valid peers by spoofing a single 12-byte `RecvError` packet with a guessed/observed `RemoteIndex` and the victim's peer's UDP source address. Repeated spoofing constitutes a persistent denial-of-service / remote state poisoning primitive against the mesh, since `closeTunnel` plus `DeleteHostInfo` discards session keys and forces a full re-handshake, disrupting traffic and enabling repeated tunnel-teardown attacks.

### Likelihood Explanation
High: `RemoteIndex` is unencrypted and present in every outgoing Nebula packet header, making it observable to any network-position attacker (or determinable via traffic analysis/timing without needing to break Noise). UDP source-address spoofing is a well-known, low-cost technique. No certificate, handshake participation, or possession of any secret is required to reach `handleRecvError` — only knowledge of the wire-visible index and the ability to spoof a UDP packet.

### Recommendation
1. Do not allow an unauthenticated 12-byte packet type to trigger destructive state changes (`closeTunnel`, `DeleteHostInfo`) based solely on `RemoteIndex` + source-address equality. Require cryptographic proof (e.g., a MAC keyed by the tunnel's session key, or embedding an authenticated tag derived from the current cipher state) before honoring a `RecvError`.
2. At minimum, rate-limit and require multiple independent unauthenticated indicators before tearing down a tunnel, and treat `RecvError` purely as an optimization hint (e.g., trigger a fresh handshake attempt) rather than an authority to immediately delete existing, keyed session state.
3. Audit all packet types dispatched in `readOutsidePackets` before hostinfo/cert validation (`Handshake`, `RecvError`) to ensure none of them can mutate authenticated session state based on unauthenticated wire data.

### Proof of Concept
1. Establish a legitimate tunnel between node `A` (victim) and node `B`, both holding valid CA-signed certificates.
2. As attacker `E` (no certificate at all), passively observe or infer `A`'s `RemoteIndex` value for the `A<->B` tunnel from the cleartext header of any packet `A` sends toward `B` (the index is not encrypted).
3. Craft a plaintext 12-byte header: `header.Encode(buf, header.Version, header.RecvError, 0, <observed_index>, 0)` and send it via UDP to `A`, spoofing the source address/port to match `B`'s known UDP endpoint (per `outside.go:528-539` this is exactly the format `sendRecvError` produces).
4. On `A`, `readOutsidePackets` routes this straight to `handleRecvError` without any certificate or Noise validation [10](#0-9) ; the address check passes because the source was spoofed to equal `hostinfo.GetRemote()` [7](#0-6) , and `A` calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the legitimate tunnel to `B` — all without `E` ever presenting a certificate.

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

**File:** outside.go (L528-539)
```go
func (f *Interface) sendRecvError(endpoint netip.AddrPort, index uint32) {
	f.messageMetrics.Tx(header.RecvError, 0, 1)

	b := header.Encode(make([]byte, header.Len), header.Version, header.RecvError, 0, index, 0)
	_ = f.outside.WriteTo(b, endpoint)
	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error sent",
			"index", index,
			"udpAddr", endpoint,
		)
	}
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

**File:** CHANGELOG.md (L188-191)
```markdown
### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)
```
