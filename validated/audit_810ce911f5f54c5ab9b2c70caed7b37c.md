### Title
Unauthenticated `recv_error` teardown lets an attacker without a CA-signed certificate forcibly close an established tunnel - ([File: outside.go])

### Summary
Nebula's `RecvError` control message is processed before any cryptographic authentication of the sender and is accepted by default (`listen.accept_recv_error: always`). The only check performed is that the source UDP address matches the currently known remote address of the local hostinfo entry for the claimed index — a value that is trivially forgeable over UDP and not bound to any certificate, MAC, or nonce. This mirrors the `registerTokenOnL2` pattern: a state-changing operation (here, tearing down an authenticated tunnel) is reachable by any network party with no ownership/identity check, only a weak, spoofable heuristic.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets straight to `f.handleRecvError` before any decryption or handshake/cert verification is required [1](#0-0) :

```
switch h.Type {
case header.Handshake:
    f.handshakeManager.HandleIncoming(via, packet, h)
    return
case header.RecvError:
    f.handleRecvError(via.UdpAddr, h)
    return
}
```

`handleRecvError` then only verifies that the sender's UDP source address equals the currently stored remote address for the hostinfo matched by `h.RemoteIndex`, and otherwise tears the tunnel down unconditionally: [2](#0-1) 

```
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		...
		return
	}
	...
	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		...
		return
	}
	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?", ...)
		return
	}
	f.closeTunnel(hostinfo)
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```

There is no signature, MAC, nonce, or cert check on the `RecvError` packet itself — it is a bare 12-byte header (`header.Encode`) with no payload authentication [3](#0-2) . The gate `ShouldRecvError` only distinguishes "always/never/private" acceptance policies and defaults to `always` [4](#0-3) [5](#0-4) . Because UDP source addresses are spoofable (or observable/known to any third party that can see the underlay traffic — e.g., a co-tenant on the same NAT/network, or any host that can send a packet with the target's source IP), an attacker holding no CA-issued Nebula certificate at all can inject a `RecvError` datagram that matches an established tunnel's remote address/index and force `closeTunnel` + `DeleteHostInfo` on the victim — exactly the "no access control on a state-mutating operation" pattern the report describes for `registerTokenOnL2`, just applied to tunnel teardown instead of L2 token registration.

### Impact Explanation
An unauthenticated network attacker can repeatedly and remotely tear down otherwise fully authenticated, established Nebula tunnels between two legitimate certificate holders, without ever presenting a valid CA-signed certificate. This is a persistent denial-of-service / connection-disruption vector against the overlay: every forged `RecvError` that matches an active hostinfo's remote/index pair destroys that tunnel state on the receiving side, forcing a full re-handshake, and can be repeated indefinitely to keep two peers from maintaining a stable tunnel (similar in spirit to how an unauthenticated `registerTokenOnL2` call permanently disrupts a legitimate bridge configuration).

### Likelihood Explanation
Likelihood is medium: the attacker needs the victim's underlay `ip:port` (learnable via traffic observation, being on the same LAN/NAT, or lighthouse discovery in some deployments) and the `RemoteIndex` value used for the specific hostinfo (a 32-bit value generated at handshake time, observable on the wire since handshake and data packets carry index values in cleartext headers). No cryptographic material or CA certificate is required at any point, and the default configuration (`listen.accept_recv_error: always`) accepts these packets unconditionally as long as the address heuristic matches.

### Recommendation
Do not act on `RecvError` (or any control message) as an unauthenticated party. At minimum:
- Require `RecvError` handling to only affect a tunnel when it can be cryptographically tied to the existing tunnel session (e.g., an authenticated/MACed control message under the derived tunnel key), not a bare unauthenticated header with a spoofable source-IP match.
- If a lightweight unauthenticated fast-path must be retained for performance, treat a matching `RecvError` only as a *hint* to accelerate a legitimate liveness check/re-handshake attempt rather than an unconditional `closeTunnel`, and rate-limit/backoff based on it.
- Change the default `listen.accept_recv_error` policy to a more conservative value (e.g., `private`/`never`) rather than `always`, and document the residual spoofing risk explicitly for operators who enable it.

### Proof of Concept
1. Attacker observes (or is co-located on the same network segment as) two nebula peers A and B with an established tunnel, and learns B's `remoteIndex` value as seen by A (visible in cleartext Nebula packet headers exchanged between A and B) and B's underlay `ip:port`.
2. Attacker crafts a minimal 12-byte Nebula header packet: `header.Encode(buf, header.Version, header.RecvError, 0, remoteIndexOfB, 0)` — no encryption or certificate involved (mirrors `sendRecvError` in `outside.go`).
3. Attacker sends this UDP datagram to A, spoofing (or naturally having) a source address equal to B's currently known remote `ip:port`.
4. A's `handleRecvError` finds the matching `hostinfo` via `QueryReverseIndex`, sees `addr == hostinfo.GetRemote()`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the legitimate, authenticated tunnel between A and B — achieved entirely without the attacker ever holding a CA-signed Nebula certificate.

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

**File:** interface.go (L132-143)
```go
func (s recvErrorConfig) ShouldRecvError(endpoint netip.AddrPort) bool {
	switch s {
	case recvErrorPrivate:
		return endpoint.Addr().IsPrivate()
	case recvErrorAlways:
		return true
	case recvErrorNever:
		return false
	default:
		panic(fmt.Errorf("invalid recvErrorConfig value: %d", s))
	}
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
