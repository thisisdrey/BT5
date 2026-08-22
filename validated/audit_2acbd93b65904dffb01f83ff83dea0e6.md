I have enough evidence now to write the analog finding.### Title
`accept_recv_error` defaults to "always", allowing unauthenticated remote teardown of established tunnels - ([File: interface.go])

### Summary
Analogous to `callRestricted` defaulting to `false` in the reported contract bug (a security-relevant gate that should be restrictive by default is instead permissive by default), Nebula's `listen.accept_recv_error` config defaults to `"always"`, meaning the daemon accepts and acts on unauthenticated `RecvError` control packets from *any* remote address before any certificate/handshake authentication has occurred. This is reachable by a network attacker with no CA-signed certificate.

### Finding Description
`RecvError` is an unencrypted, unauthenticated header-only message type (`header.RecvError`) handled in the packet dispatch path in `outside.go` before any decryption or peer authentication: [1](#0-0) 

The handler `handleRecvError` gates processing on `f.acceptRecvErrorConfig.ShouldRecvError(addr)`: [2](#0-1) 

This config defaults to `"always"` unless explicitly configured otherwise: [3](#0-2) 

`recvErrorAlways` unconditionally returns `true` for `ShouldRecvError`, regardless of source address: [4](#0-3) 

Just as `callRestricted` should default to `true` to enforce the whitelist check in `onlyWhitelistedCallee`, `accept_recv_error` defaults to the most permissive setting (`always`) instead of a safer default (`never` or `private`), meaning any off-network or on-path attacker who can send a spoofed UDP packet to the Nebula listener with a guessed/observed `RemoteIndex` can trigger the RecvError code path. The only mitigating check is a source-address comparison against the last known good remote (`hr != addr`), which is a plain UDP source-address comparison and does not involve any cryptographic authentication — it can be defeated by IP spoofing on networks that do not enforce BCP38/anti-spoofing, or trivially satisfied by an attacker that is on-path or has observed the peer's address (e.g., a NAT/relay observer).

### Impact Explanation
A successful RecvError injection causes `f.closeTunnel(hostinfo)` and deletion of handshake state, tearing down an already-established, authenticated tunnel between two legitimate Nebula peers — a remote, unauthenticated state-poisoning/denial-of-service primitive. Because the default is `"always"`, this attack surface is present in stock deployments without any explicit opt-in, exactly mirroring the disclosed bug class where a security control (`callRestricted`) is silently permissive by default and must be manually hardened by every deployer.

### Likelihood Explanation
Likelihood is moderate-to-high: `RemoteIndex` is only a 32-bit value transmitted in cleartext, and repeated normal traffic exposes it to any on-path observer (including relays, or attackers who can perform a MITM on the underlay network); no cryptographic material or valid certificate is required to construct and send a `RecvError` packet. The residual address-match check reduces — but does not eliminate — exploitability on networks that permit source-IP spoofing or where the attacker can spoof/relay from the legitimate peer's observed address.

### Recommendation
- **Short term:** Change the default for `listen.accept_recv_error` from `"always"` to a safer value such as `"never"` or `"private"`, consistent with the recommendation to make `callRestricted` default to the restrictive state. At minimum, document prominently that operators must opt in to accepting `RecvError` packets from public/untrusted networks.
- **Long term:** Consider requiring some form of lightweight proof (e.g., binding to the current session's negotiated state, or rate-limiting/challenge before honoring teardown) so that tunnel teardown cannot be triggered by an unauthenticated packet purely based on address+index matching.

### Proof of Concept
1. Establish a Nebula tunnel between hosts A and B (default config, `accept_recv_error` unset → `"always"`).
2. As attacker M, observe or infer B's `RemoteIndex` value used with A (visible in cleartext header of any packet exchanged, e.g., by being on-path or a relay).
3. Craft a bare 16-byte Nebula header packet with `Type = RecvError`, `RemoteIndex` set to the observed index, and send it via UDP to A, spoofing the source address to match B's known `udpAddr` (or send from an on-path/relay position that satisfies the `hr != addr` check).
4. A's `handleRecvError` (`outside.go:541-574`) passes the `ShouldRecvError` check (default `always`), finds the hostinfo via `QueryReverseIndex`, and calls `f.closeTunnel(hostinfo)`, tearing down the legitimate, authenticated tunnel — without M ever possessing a valid Nebula certificate.

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

**File:** outside.go (L541-574)
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
```

**File:** interface.go (L124-143)
```go
type recvErrorConfig uint8

const (
	recvErrorAlways recvErrorConfig = iota
	recvErrorNever
	recvErrorPrivate
)

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
