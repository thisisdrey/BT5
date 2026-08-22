### Title
Unauthenticated `RecvError` packets let an attacker tear down established tunnels and reset handshake state - (File: `outside.go`)

### Summary
Nebula's `header.RecvError` control message is processed and acted upon before any certificate/handshake authentication is performed, and the only anti-spoofing check is a plaintext UDP source-address comparison. An attacker with no CA-signed certificate — but who can spoof or is on-path for a peer's UDP source address and can guess/observe the numeric `RemoteIndex` — can force `closeTunnel()` and `DeleteHostInfo()` on a legitimately-authenticated tunnel between two cert holders, exactly mirroring the reported bug class where an unauthorized caller resets state to block the legitimate/authorized parties from operating.

### Finding Description
Inbound UDP packets are dispatched by type/subtype in `readOutsidePackets` before certificate-backed `ConnectionState` is required: [1](#0-0) 

`header.RecvError` is handled by `handleRecvError`, which is reached without any Noise/cert handshake validation on the packet itself — its only defenses are a config gate (`acceptRecvErrorConfig.ShouldRecvError`) and comparing the UDP source address against the hostinfo's cached remote address: [2](#0-1) 

If the source address happens to match (trivially true for an on-path or off-path spoofing attacker over UDP, since this is not a cryptographically bound check but a plain `netip.AddrPort` comparison), the function immediately calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` — tearing down the tunnel and clearing pending handshake state for an already-authenticated peer pair, without the attacker ever presenting a valid certificate or completing a handshake.

This is structurally analogous to `executeSetterFunction()` in the referenced report: a state-resetting operation (`approvedToUpdate[...] = false` there vs. `closeTunnel`/`DeleteHostInfo` here) is reachable by an unauthorized caller and used to block the legitimate/authorized parties (admins there, valid cert-holding peers here) from their intended operation.

### Impact Explanation
An attacker with no valid certificate can repeatedly send spoofed `RecvError` packets (guessing or observing 32-bit `RemoteIndex` values, which are not secret) to force teardown of established tunnels between legitimate peers, and clear pending handshake entries, denying service and forcing constant re-handshakes. This is a remote-crash/DoS-class impact reachable entirely pre-authentication (no CA-signed cert required), consistent with the "remote state poisoning / DoS" impact bar.

### Likelihood Explanation
Likelihood is moderate: the attacker needs to know or brute-force the `RemoteIndex` (32-bit, but observable by an on-path attacker sniffing traffic, or via traffic analysis) and needs the packet's apparent UDP source address to match the peer's currently recorded remote (spoofable on UDP, especially for off-path attackers behind NAT/firewalls that don't do strict egress/ingress filtering). No certificate or handshake completion is required at all, which is the key differentiator that makes this reachable by a completely unauthenticated attacker.

### Recommendation
Do not let a plaintext, unauthenticated `RecvError` packet directly tear down a tunnel. Require it to be cryptographically bound to the session (e.g., only accept `recv_error` acknowledgements as part of an authenticated/encrypted channel, or require a nonce/token issued over the encrypted tunnel and echoed back) rather than relying solely on a UDP source-address string comparison, which provides no real authentication guarantee against spoofing.

### Proof of Concept
1. Establish a legitimate tunnel between hosts A and B (valid CA-signed certs), noting B's `remoteIndexId` (learned via traffic capture or side channel).
2. From an attacker machine capable of spoofing A's UDP source address/port (or an on-path attacker), send B a single-byte-header `header.RecvError` packet with `RemoteIndex` set to the observed index, matching A's known `netip.AddrPort`.
3. `handleRecvError` on B passes the `ShouldRecvError`/address-match checks and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, terminating the legitimate tunnel between A and B — without the attacker ever presenting a certificate or completing a handshake. [2](#0-1)

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
