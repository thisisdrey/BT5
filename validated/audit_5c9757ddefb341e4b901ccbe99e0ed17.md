### Title
Unauthenticated `recv_error` teardown allows griefing of established tunnels - (File: outside.go)

### Summary
Nebula's `RecvError` message type is processed entirely outside of the AEAD/handshake trust boundary. `Interface.handleRecvError` in `outside.go` tears down an established, fully-authenticated tunnel based solely on an unauthenticated, unencrypted UDP packet whose only "proof" is a 32-bit `RemoteIndex` and a source-address match check that an off-path/spoofing attacker can satisfy without ever holding a CA-signed certificate.

### Finding Description
Inbound `RecvError` packets are dispatched before any certificate or AEAD verification: `readOutsidePackets` parses the header and, for `h.Type == header.RecvError`, immediately calls `f.handleRecvError(via.UdpAddr, h)`, bypassing the hostinfo/`ConnectionState` decrypt path used for `header.Message`/`header.LightHouse` traffic. [1](#0-0) 

`handleRecvError` looks up the victim's hostinfo purely by the attacker-supplied `RemoteIndex` (`h.RemoteIndex`) via `QueryReverseIndex`, and only additionally checks that the packet's *claimed* source address matches the hostinfo's currently known remote: [2](#0-1) 

Because `RecvError` packets carry no cryptographic authentication (no AEAD tag, no cert, no replay-window check like `ConnectionState.Decrypt`/`Bits.Check` provides for data-plane messages), an attacker who can spoof the source UDP address of the victim's known peer (trivial on UDP, no CA-signed identity required) and can guess/observe the 32-bit `RemoteIndex` (visible on the wire in every packet of that tunnel, since headers are unencrypted per the header layout) can forge a `RecvError` and have `f.closeTunnel(hostinfo)` + `f.handshakeManager.DeleteHostInfo(hostinfo)` executed against a legitimate peer's live session. This is directly analogous to the reported bug class: a cheap, unauthenticated action (Bob's 1-wei repayment) invalidates a legitimate party's in-flight state (Alice's `reduceDebt` call), causing failure/rollback of otherwise-valid work — here, the "otherwise-valid work" is an established, authenticated Nebula tunnel, and the "cheap attacker action" is a spoofed, unauthenticated `RecvError` UDP datagram.

The maintainers themselves flagged this exact class of risk in the changelog — restricting when `recv_error` is honored (`listen.accept_recv_error`) and disabling `recv_error` sends outside the allowed counter window — indicating this attack surface (unauthenticated tunnel teardown via `RecvError`) is a recognized but only partially mitigated concern. [3](#0-2) [4](#0-3) 

### Impact Explanation
An attacker with no valid certificate and no participation in the Noise handshake can force teardown of any active tunnel between two legitimate nodes by spoofing a single unauthenticated `RecvError` packet, as long as they can (a) spoof the peer's source `netip.AddrPort` and (b) know or brute-force the target's 32-bit `RemoteIndex`. This is a remote, low-cost, repeatable denial-of-service/griefing primitive against production tunnels — forcing repeated re-handshakes, disrupting availability, and (depending on `accept_recv_error` config, which defaults to `always`) doing so with essentially the cost of one crafted UDP packet. [5](#0-4) 

### Likelihood Explanation
Likelihood is constrained by two factors: the attacker must know the 32-bit `RemoteIndex` (observable by any on-path/off-path observer of the UDP traffic between the two nodes, since it's unencrypted in every header) and must spoof the UDP source address to match the hostinfo's currently-recorded remote (`hostinfo.GetRemote()`), which is feasible for an off-path attacker on networks that don't perform egress/ingress source filtering, and trivial for anyone who can observe traffic (e.g., a compromised router, shared network segment, or man-in-the-middle position) without needing the peer's private key or a CA-issued cert. `send_recv_error`/`accept_recv_error` can be set to "never" or "private" to reduce exposure, but the default (`always`) leaves the primitive live. [6](#0-5) 

### Recommendation
- Do not tear down an active `ConnectionState` based on an unauthenticated `RecvError` alone; at minimum, require some data-plane confirmation (e.g., mark the tunnel as "possibly-dead" and let the connection manager/keepalive path validate liveness before deleting the hostinfo).
- Bind `RecvError` acceptance to a recent, session-scoped token (e.g., only accept a `RecvError` whose counter/index correlates to a message actually sent in a very recent window, similar to how the changelog fix restricted sending `recv_error` outside the counter window — the same restriction should also gate *acceptance*, not just transmission).
- Default `listen.accept_recv_error` to `private`/`never` rather than `always`, or otherwise require corroborating signals (e.g., number of consecutive real `RecvError`s, rate limiting) before tearing down a tunnel.

### Proof of Concept
1. Observe (or otherwise learn) the `RemoteIndex` used by Victim-A's active tunnel to Victim-B by sniffing any packet exchanged between them (indices are sent in cleartext in every header per `header.Encode`/`header.H.Parse`). [7](#0-6) 
2. Craft a UDP datagram with `header.Type = RecvError`, `RemoteIndex` set to the observed index, and source address spoofed to match Victim-B's currently known remote address for that tunnel (`hostinfo.GetRemote()`).
3. Send the spoofed packet to Victim-A. `readOutsidePackets` routes it straight to `f.handleRecvError` without any decrypt/cert check. [8](#0-7) 
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex(h.RemoteIndex)`, sees the spoofed source matches `hr`, and calls `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo(hostinfo)`, tearing down Victim-A's live, authenticated tunnel to Victim-B — with no certificate, no handshake, and one UDP packet. [9](#0-8) 

Note: I could not fully verify from the indexed code how predictable/guessable `RemoteIndex` values are in practice (the `generateIndex` implementation wasn't fully retrieved), which affects the practical difficulty of step 1 beyond passive observation; a full Devin session with complete file access would be needed to confirm index-generation randomness/entropy characteristics.

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

**File:** CHANGELOG.md (L188-191)
```markdown
### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)
```

**File:** CHANGELOG.md (L525-529)
```markdown
### Security

- You can set `listen.send_recv_error` to control the conditions in which
  `recv_error` messages are sent. Sending these messages can expose the fact
  that Nebula is running on a host, but it speeds up re-handshaking. (#670)
```

**File:** interface.go (L436-457)
```go
func (f *Interface) reloadSendRecvError(c *config.C) {
	if c.InitialLoad() || c.HasChanged("listen.send_recv_error") {
		stringValue := c.GetString("listen.send_recv_error", "always")

		switch stringValue {
		case "always":
			f.sendRecvErrorConfig = recvErrorAlways
		case "never":
			f.sendRecvErrorConfig = recvErrorNever
		case "private":
			f.sendRecvErrorConfig = recvErrorPrivate
		default:
			if c.GetBool("listen.send_recv_error", true) {
				f.sendRecvErrorConfig = recvErrorAlways
			} else {
				f.sendRecvErrorConfig = recvErrorNever
			}
		}

		f.l.Info("Loaded send_recv_error config", "sendRecvError", f.sendRecvErrorConfig.String())
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

**File:** header/header.go (L91-98)
```go
type H struct {
	Version        uint8
	Type           MessageType
	Subtype        MessageSubType
	Reserved       uint16
	RemoteIndex    uint32
	MessageCounter uint64
}
```
