## Title
Unauthenticated `RecvError` message allows any off-path attacker to force-teardown an arbitrary established tunnel by spoofing index and source address - (File: outside.go)

### Summary
The reference finding describes `LenderPool.withdrawInterest()` accepting an arbitrary `_lender` identifier and performing a state-changing action on behalf of that party without checking that the caller is actually the affected party. The structural analog in nebula is `Interface.handleRecvError`, which processes an entirely unauthenticated, unencrypted control message (`header.RecvError`) that acts on a victim `HostInfo` identified only by a 32-bit `RemoteIndex` value, without any cryptographic proof that the sender is the actual peer for that tunnel.

### Finding Description
`RecvError` is one of only two message types handled entirely before decryption/authentication (`outside.go` `readOutsidePackets`, lines 76-84):

```go
case header.RecvError:
    f.handleRecvError(via.UdpAddr, h)
    return
``` [1](#0-0) 

`handleRecvError` looks up the target tunnel purely by `h.RemoteIndex` (a value carried in cleartext in every nebula packet header) and only "authenticates" the request by comparing the observed UDP source address against the hostinfo's last-known remote address:

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
``` [2](#0-1) 

No cryptographic material (no HMAC, no noise-derived key, no counter/replay window) protects this message; it's a bare 8-byte header comparable to how `sendRecvError` constructs it (`header.Encode(..., header.RecvError, 0, index, 0)`), sent with `f.outside.WriteTo(b, endpoint)`. [3](#0-2) 

Both `RemoteIndex` (visible on every packet exchanged between the two legitimate peers) and the current UDP `ip:port` of a peer are observable by anyone who can see the victim's traffic (e.g. on-path observer, or an attacker who can otherwise learn/guess these values), and the UDP source address itself is trivially spoofable since UDP performs no source verification at the transport layer. This is the same pattern as the `withdrawInterest` bug: the function performs an authoritative action ("tear down this specific tunnel") on behalf of an identified party (the hostinfo keyed by `RemoteIndex`) using only weak, attacker-controllable/observable identifiers as the sole access-control check, rather than verifying the request originates from a party holding the tunnel's actual cryptographic material.

### Impact Explanation
An attacker who can spoof UDP source packets to a nebula node (a well-known and inherent property of UDP, requiring no CA-signed certificate) and who has observed a target's `RemoteIndex` (visible in every plaintext nebula header exchanged over the wire) and current remote `ip:port` can force `closeTunnel`+`DeleteHostInfo` for that victim's established tunnel, mirroring the "remote state poisoning" impact bucket. This causes involuntary tunnel teardown / denial-of-service and triggers a costly re-handshake cycle, without the attacker ever needing to complete a handshake or hold a trusted certificate — the underlying design flaw is functionally identical to `withdrawInterest`'s missing "is caller the affected party" check.

### Likelihood Explanation
Nebula's own CHANGELOG documents this class of concern being partially mitigated over time (e.g. "Disable sending recv_error messages when a packet is received outside the allowable counter window" #1459, and the `listen.accept_recv_error`/`listen.send_recv_error` config knobs defaulting to `always`), which confirms the historical existence and awareness of this exposure surface. [4](#0-3) [5](#0-4) 
By default (`recvErrorAlways`), the interface accepts and acts on `RecvError` from any address with no additional protection beyond the address/index match check shown above, so exploitation likelihood under default configuration is non-trivial for an attacker capable of UDP source spoofing plus observation of ongoing traffic between two peers.

### Recommendation
Do not act on `RecvError` (or any control-plane message affecting a specific hostinfo/tunnel) based solely on a spoofable UDP source address and a predictable/observable index. At minimum, require the message to be either (a) authenticated using tunnel-derived key material once a tunnel exists, or (b) rate-limited and cross-checked against additional session state that an off-path spoofer cannot know, and default `listen.accept_recv_error` to a safer mode (e.g. `private`/`never`) rather than `always`.

### Proof of Concept
1. Passively observe UDP traffic between victim node `A` and peer `B` (or otherwise learn `A`'s current `RemoteIndex` value used by `B`, and `A`'s active remote `ip:port` for `B`'s tunnel — both visible in plaintext nebula headers on the wire).
2. Craft a minimal 8-byte nebula header with `Type = header.RecvError`, `RemoteIndex` set to the value used by `B`↔`A` tunnel (as constructed in `sendRecvError`, `outside.go` lines 528-539).
3. Spoof the UDP source address to `B`'s observed `ip:port` and send the packet to `A`.
4. `A`'s `readOutsidePackets` routes the packet to `handleRecvError` (`outside.go` line 82) before any decryption; the address check passes because it only compares against `hostinfo.GetRemote()` (which equals `B`'s address, the spoofed source), and `A` calls `closeTunnel(hostinfo)` + `DeleteHostInfo(hostinfo)`, tearing down the legitimate `A↔B` tunnel without any involvement from `B` or possession of a valid certificate by the attacker.

Note: I was unable to fully verify from the indexed code whether `RemoteIndex` values are otherwise protected from observation in any deployment scenario (e.g., encrypted transport tunneling nebula itself), so the practical exploitability depends on the attacker's network vantage point; a Devin session with full file/repo access would be needed to check for any additional mitigations (e.g., token-bucket rate limiting per index) not captured in the indexed snippets.

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

**File:** CHANGELOG.md (L181-191)
```markdown
## [1.9.7] - 2025-10-10

### Security

- Fix an issue where Nebula could incorrectly accept and process a packet from an erroneous source IP when the sender's
  certificate is configured with unsafe_routes (cert v1/v2) or multiple IPs (cert v2). (#1494)

### Changed

- Disable sending `recv_error` messages when a packet is received outside the allowable counter window. (#1459)
- Improve error messages and remove some unnecessary fatal conditions in the Windows and generic udp listener. (#1453)
```

**File:** interface.go (L436-480)
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
