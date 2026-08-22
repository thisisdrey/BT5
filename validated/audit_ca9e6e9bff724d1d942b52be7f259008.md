### Title
Unauthenticated `RecvError` Packet Allows Any Off-Path Attacker to Tear Down an Established Tunnel by Spoofing the Sender Address - (File: `outside.go`)

### Summary
`RecvError` is one of only two unencrypted, pre-handshake message types nebula will act on (`header.RecvError`, handled before any certificate/AEAD verification), the other being `Handshake` itself. [1](#0-0)  Its handler, `handleRecvError`, tears down an established tunnel (`closeTunnel` + `DeleteHostInfo`) based solely on a `RemoteIndex` value taken from the plaintext header and a UDP source-address comparison against the last known remote for that index — no certificate, no CA-pool check, no AEAD authentication of any kind is involved. [2](#0-1)  This mirrors the Vader `mintFungible()` pattern: a privileged state-mutating operation (tunnel teardown) is performed on the strength of an unauthenticated, attacker-suppliable identifier rather than a verified identity.

### Finding Description
In `readOutsidePackets`, the dispatch on header type happens immediately after basic header parsing and before any certificate-backed decrypt/verify step:
```go
switch h.Type {
case header.Handshake:
    f.handshakeManager.HandleIncoming(via, packet, h)
    return
case header.RecvError:
    f.handleRecvError(via.UdpAddr, h)
    return
}
``` [1](#0-0) 

`handleRecvError` then does:
```go
hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
...
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
``` [3](#0-2) 

The only "authorization" check is that the UDP source address of the packet (`addr`, taken directly and unauthenticated from the incoming datagram) equals the last-known remote address recorded for that `RemoteIndex`. Both of the values needed to pass this check are visible in cleartext:
- `RemoteIndex` is a plaintext 4-byte field present in every nebula packet header (`header.H.RemoteIndex`), sent unencrypted on the wire. [4](#0-3) [5](#0-4) 
- The peer's UDP `ip:port` is likewise visible to anyone who can observe or infer the traffic (e.g., a network sniffer on the path, or a peer that simply knows/guesses the target's public endpoint).

Because UDP has no origin authentication, an attacker who can observe (or spoof source addresses toward) that `ip:port` pair can send a bare, unencrypted `RecvError` packet carrying the observed `RemoteIndex` and have it accepted at face value — with no CA-signed certificate, no completed handshake, and no cryptographic proof of identity whatsoever. This exactly parallels the Vader finding: `mintFungible()` trusted an unauthenticated caller-supplied argument (the `to` recipient) to control a sensitive state transition; here `handleRecvError` trusts an unauthenticated, attacker-suppliable identifier (`RemoteIndex` + spoofable source `addr`) to control a sensitive state transition (tunnel teardown).

The developers were clearly aware `RecvError` is sensitive — it is gated by a config option (`listen.accept_recv_error`, default `always`) precisely because "sending these messages can expose the fact that nebula is running on a host" and can be abused, and the changelog shows prior hardening (`Disable sending recv_error messages when a packet is received outside the allowable counter window`). [6](#0-5) [7](#0-6)  Despite that awareness, the *accept* path still authorizes the teardown using only a plaintext, non-cryptographic address-match check.

### Impact Explanation
This is a remote, unauthenticated denial-of-service / remote state poisoning primitive:
- An attacker with no nebula certificate and no established session can force teardown of any other host's active tunnel by replaying/spoofing a single unencrypted UDP packet, as long as they know (or can observe) the victim's public `ip:port` and the numeric `RemoteIndex` currently in use (visible in the clear on every packet of that tunnel).
- Repeated at will, this enables persistent disruption of connectivity between two legitimate, fully-authenticated nebula peers — defeating the purpose of the mutual-certificate-authenticated tunnel model, since teardown does not require possessing a valid certificate at all.
- `closeTunnel`/`DeleteHostInfo` removes the hostinfo from both the main and pending hostmaps, forcing a full re-handshake, which is exactly the kind of "remote state poisoning" impact called out as in-scope.

### Likelihood Explanation
- `RecvError` acceptance defaults to `always` (`recvErrorAlways`), so this path is enabled out-of-the-box on default configuration. [8](#0-7) 
- `RemoteIndex` is not a secret; it is transmitted unencrypted in every packet header of a live tunnel, so any attacker positioned to observe even one packet (or who is told/guesses the endpoint) obtains everything needed. [4](#0-3) 
- The only gate is a plaintext source-address equality check, which is trivially satisfiable by any attacker capable of UDP source spoofing, or by any on-path/off-path observer relaying from the true source address, and is entirely bypassed if `hr.IsValid()` is false (e.g., before the victim has learned/recorded a remote for that hostinfo). [9](#0-8) 

### Recommendation
Do not allow an unauthenticated, unencrypted packet type to trigger destructive session-state changes based purely on a plaintext index/address match. Options:
1. Require `RecvError` handling to additionally validate a value that only the legitimate peer could produce (e.g., an authenticated tag over the `RemoteIndex` derived from the session's established keys), rather than relying solely on address comparison.
2. At minimum, require stronger corroboration before tearing down a tunnel (e.g., require multiple consistent `RecvError` signals over time, or bound teardown to only occur when `hr.IsValid()` and it matches — never treat an unset/invalid remote as an implicit "allow").
3. Consider defaulting `listen.accept_recv_error` to a more conservative value (e.g., `private`/`never`) rather than `always`, and clearly document that `RecvError` is not authenticated and is spoofable teardown vector on the open Internet.

### Proof of Concept
Conceptual PoC (network-level, no code execution required):
1. Observe (via passive sniffing, or by knowing the endpoint from configuration/lighthouse data) a single nebula UDP packet exchanged between victim host A and its peer B, and extract the plaintext `RemoteIndex` field from the header (`header.H.RemoteIndex`, bytes 4–8 of every packet). [5](#0-4) 
2. Craft a minimal UDP datagram: `header.Encode(buf, header.Version, header.RecvError, 0, capturedRemoteIndex, 0)` and send it to A's UDP listening port, spoofing (or legitimately using, if reachable) B's source `ip:port`. [10](#0-9) 
3. On receipt, A's `handleRecvError` resolves `hostinfo` via `QueryReverseIndex(RemoteIndex)`, finds the source address matches (or `hr` invalid), and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the legitimate, cert-authenticated tunnel between A and B — without the attacker ever presenting a CA-signed certificate or completing any handshake. [3](#0-2) 

Note: I was not able to fully verify whether any additional network-layer protections (e.g., default firewall/NAT behavior in typical deployments, or unverified assumptions about attacker's ability to spoof UDP source addresses in the target's network path) reduce practical exploitability in all deployment scenarios; this assessment is based purely on the application-layer logic in the repository.

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

**File:** header/header.go (L143-156)
```go
func (h *H) Parse(b []byte) error {
	if len(b) < Len {
		return ErrHeaderTooShort
	}
	// get upper 4 bytes
	h.Version = uint8((b[0] >> 4) & 0x0f)
	// get lower 4 bytes
	h.Type = MessageType(b[0] & 0x0f)
	h.Subtype = MessageSubType(b[1])
	h.Reserved = binary.BigEndian.Uint16(b[2:4])
	h.RemoteIndex = binary.BigEndian.Uint32(b[4:8])
	h.MessageCounter = binary.BigEndian.Uint64(b[8:16])
	return nil
}
```

**File:** interface.go (L124-156)
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

func (s recvErrorConfig) String() string {
	switch s {
	case recvErrorAlways:
		return "always"
	case recvErrorNever:
		return "never"
	case recvErrorPrivate:
		return "private"
	default:
		return fmt.Sprintf("invalid(%d)", s)
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

**File:** CHANGELOG.md (L188-192)
```markdown
### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)

```
