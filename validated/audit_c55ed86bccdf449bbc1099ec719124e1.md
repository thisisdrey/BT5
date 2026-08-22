### Title
Unauthenticated `recv_error` packets allow a network attacker with no CA-signed certificate to force remote tunnel teardown — (File: outside.go)

### Summary
The underlying keep-core defect was that a caller could supply an *arbitrary, unauthenticated selector* (contract+method) that a privileged component would blindly act on, letting anyone trigger internal state transitions in unrelated contracts. Nebula has an analogous pattern in its cleartext, pre-handshake control channel: `header.RecvError` packets are processed and acted upon (tearing down an existing, unrelated tunnel) based solely on an attacker-supplied 32-bit index and a matching source `netip.AddrPort` — no certificate, no cryptographic authentication, and no proof that the sender is actually a party to that tunnel.

### Finding Description
In `readOutsidePackets`, before any certificate/handshake state is required, packets of type `header.RecvError` are routed directly to `f.handleRecvError`: [1](#0-0) 

`handleRecvError` looks up an existing, already-established `HostInfo` purely from the attacker-controlled `h.RemoteIndex` field (via `QueryReverseIndex`), and if the UDP source address of the incoming packet happens to match the `HostInfo`'s currently known remote address, it unconditionally tears down that tunnel: [2](#0-1) 

Crucially:
- `h.RemoteIndex` is not a secret. It is the plaintext `RemoteIndex` field embedded in every Nebula packet header (`header.H`), so any attacker capable of observing traffic to/from either tunnel endpoint (a passive on-path observer, or simply anyone who can see UDP packets addressed to a target's listening port) learns valid index values without ever presenting a certificate.
- The only "authentication" check is that the spoofed packet's source address matches the current known remote UDP address of the victim tunnel — this is a standard IP/UDP source, which is straightforward to spoof unless the network path enforces anti-spoofing (BCP38), which is not guaranteed and is outside Nebula's control.
- No handshake, no certificate, and no CA pool are consulted at all in this code path — it fires directly off `h.Type == header.RecvError`, before any of the certificate/handshake logic in `handshake_manager.go` is reached.

This mirrors the report's bug class: an unauthenticated, attacker-chosen selector (`callbackContract`/`callbackMethod` in keep-core; `RemoteIndex`+spoofed source address here) is used to trigger a privileged state-changing action (`executeCallback` in keep-core; `closeTunnel` + `DeleteHostInfo` here) against a target the attacker does not control and has not authenticated to.

### Impact Explanation
An attacker with no CA-signed certificate can force termination of any active Nebula tunnel between two legitimate, certificate-holding peers by sending a single crafted, unauthenticated UDP packet with a guessed/observed `RemoteIndex` and a spoofed source address. This is a remote state-poisoning / denial-of-service primitive against the mesh: repeated forged `recv_error` packets can be used to continuously disrupt tunnels, degrading availability for legitimate nodes without ever obtaining a valid certificate.

### Likelihood Explanation
The `RemoteIndex` is transmitted unencrypted in every Nebula packet header, so any network position that can observe UDP traffic to or from a target (including off-path attackers on shared/broadcast segments, or anyone able to source-spoof UDP toward the victim) can harvest valid indices. Combined with UDP source-address spoofing (commonly feasible absent egress/ingress filtering), the attack requires no cryptographic material and no prior interaction with the CA — only network positioning. Nebula's own `listen.send_recv_error` / `listen.accept_recv_error` config knobs (see `CHANGELOG.md`, "You can set `listen.send_recv_error`... Sending these messages can expose the fact that Nebula is running on a host") acknowledge this feature's inherent unauthenticated exposure, but the address-match check is not a substitute for authentication.

### Recommendation
- Require that `recv_error` handling only tear down tunnels when the message can be tied to genuine packet loss the sender itself experienced (e.g., only accept a `recv_error` if it is itself authenticated, such as being sent inside an already-established, encrypted channel, or bound to a value that isn't observable in cleartext on the wire).
- Consider disabling processing of `recv_error` by default (`listen.accept_recv_error=false`) or rate-limiting/requiring additional proof-of-work/challenge before acting on it.
- At minimum, don't rely solely on source-address equality as a substitute for cryptographic authentication of the teardown request.

### Proof of Concept
1. Attacker observes (or brute forces, given 32-bit space and no rate limiting) the `RemoteIndex` used between victim host A and B by sniffing any Nebula UDP packet on the path (the field is in the cleartext header, requires no certificate).
2. Attacker spoofs a UDP packet with source address equal to B's known address and destination A's listening port, with `header.Type = header.RecvError` and `RemoteIndex` set to the observed value (see packet construction in `handleRecvError`/`sendRecvError`, `outside.go` lines 528-539 for the wire format).
3. A receives the packet, `QueryReverseIndex` resolves it to the live `HostInfo` for the tunnel with B, the spoofed source matches the recorded remote address, and `f.closeTunnel(hostinfo)` plus `DeleteHostInfo` are invoked — tearing down a legitimate tunnel with no certificate ever presented by the attacker.

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
