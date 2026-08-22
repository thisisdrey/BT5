I have sufficient evidence now to produce a final, validated answer.

### Title
Unauthenticated `RecvError` packets allow off-path attackers to force tunnel teardown - (File: outside.go)

### Summary
`handleRecvError()` accepts a bare, unencrypted, unauthenticated `header.RecvError` message type and, if the claimed source UDP address matches the currently recorded remote for the targeted `HostInfo`, immediately tears down the tunnel (`f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`). This is the same bug class as the reported `updateFairLaunchProperties()` issue: a state-mutating operation (here, destroying an authenticated tunnel/session) is reachable and actionable by a party who has not proven any relationship to the session — no CA-signed certificate, no Noise handshake, no AEAD authentication tag — only knowledge/guessing of a 32-bit index and a spoofable source `IP:port`.

### Finding Description
In `outside.go`'s `readOutsidePackets()`, `header.RecvError` is dispatched to `handleRecvError()` *before* any certificate or session state is required [1](#0-0) . `handleRecvError()` then:
1. Checks a config gate (`acceptRecvErrorConfig`), which defaults to `"always"` [2](#0-1) [3](#0-2) .
2. Looks up the `HostInfo` purely from the packet's `RemoteIndex` field via `QueryReverseIndex` [4](#0-3) .
3. Compares the packet's *UDP source address* (`addr`, fully attacker-controlled/spoofable over UDP) against the stored remote address, and if it matches (or if no remote is currently recorded), proceeds to tear the tunnel down [5](#0-4) .

There is no cryptographic authentication of the `RecvError` message itself — it carries only an 8-byte header (`header.Encode`) with no AEAD tag [6](#0-5) . Contrast this with every other state-mutating packet type (`CloseTunnel`, `Message`, `Test`, `Control`), which is only processed after `hostinfo.ConnectionState.Decrypt()` succeeds — i.e., after proving possession of the session's negotiated key, itself derived from a certificate-authenticated Noise handshake [7](#0-6) . `RecvError` is explicitly carved out of that authenticated path.

### Impact Explanation
An attacker with no valid certificate, and not on-path (able to spoof UDP source addresses, which is common for UDP over many networks, or on-path attackers), can force a remote peer to tear down an established, legitimate Nebula tunnel by sending a single crafted `RecvError` packet with a guessed/observed `RemoteIndex` and a spoofed source `IP:port` matching the peer's recorded remote. This is a remote denial-of-service / forced re-handshake against arbitrary victims in the mesh, without needing to hold a CA-signed certificate or complete any handshake.

### Likelihood Explanation
The codebase's own CHANGELOG documents this as a known, previously-accepted risk area: `listen.send_recv_error`/`listen.accept_recv_error` were added specifically because "Sending these messages can expose the fact that Nebula is running on a host" and to let operators control "the conditions in which recv_error messages are sent/accepted" [8](#0-7) [9](#0-8) . However, the default configuration for accepting `RecvError` is `"always"` [10](#0-9) , meaning out-of-the-box installations are exposed. The `RemoteIndex` is a 32-bit value assigned by `generateIndex`/`allocateIndex` and is visible in the clear on the wire during handshakes an attacker can observe if they can see any traffic on the path, making a matching guess/replay feasible for an attacker who can also spoof the peer's UDP source address.

### Recommendation
Do not let an unauthenticated `RecvError` packet unilaterally tear down a tunnel based solely on index + spoofable source address matching. Options:
- Require some proof tied to the current session (e.g., only honor `RecvError` if it echoes a recently-sent authenticated packet's counter/nonce, or authenticate it similarly to relay frames via `VerifyRelay`-style AEAD tagging keyed off the existing session).
- At minimum, change the default `listen.accept_recv_error` to a stricter mode (e.g., `private`/`never`) and require explicit opt-in for `"always"`, and rate-limit/backoff repeated `RecvError`-triggered teardowns per remote index to blunt spoofed-address DoS.

### Proof of Concept
1. Observe (or brute-force) a victim's currently active `RemoteIndex` for a tunnel to peer B (visible in handshake headers as `RemoteIndex`/`LocalIndex` fields in cleartext during the handshake, or via traffic analysis).
2. Spoof a UDP packet with source address equal to peer B's known/observed UDP endpoint, destined to the victim's listen port, containing an 8-byte `header.RecvError` header (`header.Encode(..., header.RecvError, 0, <victim's local index for the B tunnel>, 0)`), as constructed by `sendRecvError()` [6](#0-5) .
3. On receipt, `handleRecvError()` finds the matching `HostInfo` and the spoofed source matches `hostinfo.GetRemote()`, so it calls `f.closeTunnel(hostinfo)` and deletes handshake state — destroying a fully-established, certificate-authenticated tunnel with a single unauthenticated, forged packet [11](#0-10) .

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

**File:** outside.go (L96-132)
```go
	// At this point we should have a valid existing tunnel, verify and send
	// recvError if necessary
	if hostinfo == nil || hostinfo.ConnectionState == nil {
		if !via.IsRelayed {
			f.maybeSendRecvError(via.UdpAddr, h.RemoteIndex)
		}
		return
	}

	if len(packet) < header.Len+hostinfo.ConnectionState.dKey.Overhead() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("packet too small", "from", via, "length", len(packet))
		}
		return
	}

	// All remaining packets are encrypted
	if isMessageRelay {
		// Relay packets are special, this branch should always early-return
		if err = hostinfo.ConnectionState.VerifyRelay(f.l, h.MessageCounter, packet, nb); err != nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("Failed to verify relay packet", "error", err, "from", via, "header", h)
			}
			return
		}
		f.handleOutsideRelayPacket(hostinfo, via, out, packet, h, fwPacket, lhf, nb, q, localCache)
		return
	}

	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
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

**File:** outside.go (L557-574)
```go
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

**File:** CHANGELOG.md (L128-131)
```markdown
### Added

- Add a config option to control accepting `recv_error` packets which defaults to `always`. (#1569)

```

**File:** CHANGELOG.md (L525-530)
```markdown
### Security

- You can set `listen.send_recv_error` to control the conditions in which
  `recv_error` messages are sent. Sending these messages can expose the fact
  that Nebula is running on a host, but it speeds up re-handshaking. (#670)

```
