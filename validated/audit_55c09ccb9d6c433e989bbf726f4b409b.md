### Title
Unauthenticated `RecvError` packets allow anonymous remote tunnel teardown (analog of anonymous no-op-callable entry point) - ([File: outside.go])

### Summary
The Kinto report flags that `EntryPoint.sol`'s `handleOps`/`handleAggregatedOps` can be invoked by any anonymous caller with empty arguments, executing a "no-op" exploitable code path with no authentication. In nebula, the analogous reachable-without-certificate surface is the `header.RecvError` message type, which is processed in the clear (no Noise handshake, no AEAD, no CA-signed certificate check) directly off the wire in `readOutsidePackets`, and dispatched to `handleRecvError` before any cryptographic authentication occurs.

### Finding Description
In `readOutsidePackets`, packets are only length/version/subtype validated before being switched on `h.Type`. The `header.RecvError` type is handled immediately, prior to any certificate or key‑based authentication: [1](#0-0) 

`handleRecvError` accepts the *cleartext* header fields (`h.RemoteIndex`, sender `addr`) with no signature, no HMAC and no certificate verification of the sender's identity. Its only "authentication" is (1) an accept policy check and (2) a comparison of the observed UDP source `addr` against the `hostinfo.GetRemote()` value cached from a prior legitimate exchange: [2](#0-1) 

If those checks pass (which only requires knowledge of the remote index and successfully spoofing the peer's UDP source `ip:port`, both of which are observable on the wire since Nebula headers are unencrypted), the tunnel is torn down and the pending handshake state deleted — a real state-changing effect, produced by a packet that carries no CA-signed certificate at all, analogous to an anonymous caller invoking a function that "does something" (or in Kinto's case, a no-op) without holding any credential.

The `sendRecvError` counterpart is likewise emitted in the clear with no authentication of the triggering condition: [3](#0-2) 

### Impact Explanation
An attacker who can spoof UDP source addresses (trivial for UDP, no three-way handshake) and who observes/guesses a peer's `RemoteIndex` (small integer space, and often observable from prior legitimate traffic on the path) can forge a `RecvError` packet toward one side of an established Nebula tunnel. This tears down the tunnel and clears the pending handshake state for that host, causing denial-of-service/remote state poisoning — with **zero cryptographic authentication**, matching the reachability class in the bug report ("anonymous user ... can call ... which could become a problem" / "exploitable contract that can be called anonymously from any account").

### Likelihood Explanation
This is only exploitable by an attacker capable of source-IP spoofing on the path to a Nebula node (or on-path/off-path spoofing where egress filtering (BCP38) is not enforced) and correctly guessing/observing the remote index. It is mitigated by rate-limiting/accept policy (`ShouldRecvError`) and the requirement that the spoofed source match the peer's currently cached remote address — narrowing but not eliminating the window, similar to the report's framing that the exposure is most relevant in constrained/early-network conditions (here: networks with permissive routing that don't filter spoofed UDP source addresses).

### Recommendation
- Treat `RecvError` as an unauthenticated hint only; do not let it directly tear down `HostInfo`/pending-handshake state. Instead, require it to trigger nothing stronger than a probe/re-handshake attempt, or bind acceptance to data available only to the legitimate peer (e.g., verify against a recent authenticated packet counter/nonce rather than just the UDP source tuple).
- Consider authenticating `RecvError` (e.g., a lightweight MAC keyed off ephemeral tunnel material) to prevent blind/off-path spoofing from producing state changes.
- Rate-limit and log `RecvError`-triggered teardown events distinctly so operators can detect spoofing-based DoS attempts, and document this exposure for operators deploying on networks without egress/ingress source-address filtering.

### Proof of Concept
1. Establish a Nebula tunnel between host A and host B; note B's `localIndexId` (visible as `RemoteIndex` in packets B sends to A) and B's currently registered UDP endpoint on A (`hostinfo.GetRemote()`).
2. From an attacker-controlled host with the ability to spoof UDP source addresses as B's `ip:port`, send a bare Nebula header packet to A with `Type = header.RecvError` and `RemoteIndex` set to B's known index (no encryption, no certificate, no handshake required):
   - `header.Encode(buf, header.Version, header.RecvError, 0, <B's index>, 0)`
3. A's `handleRecvError` receives the packet, finds the matching `hostinfo` via `QueryReverseIndex`, sees the spoofed source matches `hr`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` — tearing down the legitimate tunnel between A and B, with the attacker never presenting a CA-signed certificate or completing any handshake. [2](#0-1)

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
