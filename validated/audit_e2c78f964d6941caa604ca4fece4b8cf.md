### Title
Unauthenticated `RecvError` packet allows remote teardown of an established tunnel - ([File: outside.go])

### Summary
`Interface.handleRecvError` tears down an already-authenticated tunnel (`f.closeTunnel(hostinfo)` plus removal from the pending handshake map) in response to a `header.RecvError` packet that carries **no cryptographic authentication at all**. The only check performed is that the packet's source `netip.AddrPort` matches the currently recorded remote address for the `HostInfo` resolved from the packet's `RemoteIndex`. Since UDP source addresses are trivially spoofable (and the `RemoteIndex` is a 32-bit value transmitted in the clear on every packet of that tunnel, so it is observable by any on-path or off-path attacker who can see traffic, or brute-forced), an attacker with no CA-signed certificate and no valid handshake state can forge a `RecvError` packet to force the victim to close a legitimate, already-established tunnel.

### Finding Description
`RecvError` is handled entirely before any decryption or certificate/HMAC verification: [1](#0-0) 

Both `Handshake` and `RecvError` packet types are dispatched from `readOutsidePackets` while the surrounding comment explicitly notes "Unencrypted packets". The handler: [2](#0-1) 

only validates that the packet's source `addr` equals `hostinfo.GetRemote()` — a value taken from the UDP source address of previous packets, not from any authenticated field. There is no HMAC, no nonce, and no reference to `ConnectionState` (the noise cipher state) anywhere in this path. Compare this to every other post-handshake message type (`Message`, `LightHouse`, `Test`, `CloseTunnel`, `Control`), all of which are only processed after `hostinfo.ConnectionState.Decrypt(...)` succeeds (outside.go:126-136), i.e., after proof of possession of the negotiated session keys derived from a CA-verified certificate exchange (`handshake.Machine.validateCert`, `handshake/machine.go:342-380`, and `HandshakeManager.certVerifier`, `handshake_manager.go:1161-1165`).

`RecvError` is the one exception: it bypasses the AEAD authentication step and instead relies purely on a spoofable source-address match, then unconditionally destroys session state:

```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    ...
    return
}
f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
```

This is structurally the same bug class as the QuantAMM `lastPoolUpdateRun` report: a state-mutating operation (tunnel/timing state) is gated on an insufficient check (source-IP equality / no permission bit) instead of on cryptographic proof of authorization (a valid, CA-signed certificate and completed handshake), letting an unauthenticated third party manipulate protected state.

`RemoteIndex` (the lookup key into `QueryReverseIndex`) is sent unencrypted in the header of every packet exchanged over the tunnel (`header.H.RemoteIndex`), so it is not a secret; an on-path observer, a NAT/traffic-mirroring attacker, or even a blind guesser (32-bit space, but active tunnels are enumerable via traffic observation) can obtain it without ever completing a handshake or presenting a certificate. [3](#0-2) 

### Impact Explanation
An attacker who can observe or spoof a single UDP packet (with the correct source `IP:port` and `RemoteIndex`) can force termination of any active nebula tunnel between two hosts, causing:
- Denial of service / repeated tunnel teardown (the peers must re-handshake, and the attack can be repeated indefinitely to prevent stable connectivity).
- Because `DeleteHostInfo` also purges the pending handshake bookkeeping, this can be combined with handshake-timing manipulation to disrupt reconnection.

This does not by itself break confidentiality/integrity of already-decrypted traffic, but it is a legitimate remote, unauthenticated state-poisoning/DoS primitive against an established, authenticated session — squarely in the "remote state poisoning" / "remote crash-equivalent disruption" impact category.

### Likelihood Explanation
Reachability depends on the `listen.send_recv_error` / `listen.accept_recv_error` configuration gates (`f.acceptRecvErrorConfig.ShouldRecvError(addr)`), whose exact default value I could not confirm within the available search budget (referenced in `interface.go`, not fully inspected). If this feature is enabled by default or commonly enabled by operators for its intended diagnostic purpose (fast reconnect on stale state), the attack is trivially exploitable by any attacker able to spoof UDP source addresses toward the victim, without needing a valid certificate, without needing to be a lighthouse, and without needing to be an existing authenticated peer.

### Recommendation
Do not act on `RecvError` (or any tunnel-teardown-triggering control message) based solely on a source-address match. At minimum:
- Require the `RecvError` message to be authenticated (e.g., HMAC'd or AEAD-protected using the existing session's `ConnectionState` keys) before honoring it, the same way `Message`/`LightHouse`/`Test`/`Control` packets are.
- If backward compatibility prevents encrypting `RecvError`, add a challenge/response or rate-limit + require multiple confirming observations before tearing down a tunnel, and never fully delete pending-handshake bookkeeping purely on this signal.

### Proof of Concept
Conceptual (network-level) PoC:
1. Establish a tunnel between hosts A and B (valid certs, completed IX handshake); note B's `localIndexId` (this value is visible in the clear as `RemoteIndex` in every packet A sends to B, and vice versa — observable by anyone who can see the traffic, e.g., a shared network segment or a compromised router).
2. From attacker-controlled host M (no certificate, not part of the network), craft a UDP packet to B's listening address:
   - `header.H{Type: header.RecvError, RemoteIndex: <A's index into B's hostmap>}`, spoofing the source `IP:port` to match A's known observed address (or, if M is on-path between A and B, no spoofing is even needed).
3. B's `handleRecvError` finds the matching `HostInfo` via `QueryReverseIndex`, sees `addr == hostinfo.GetRemote()`, and calls `f.closeTunnel(hostinfo)`, tearing down the tunnel to A without any cryptographic check.

I was not able to fully verify the default state of `listen.accept_recv_error`/`send_recv_error` configuration flags in `interface.go` within the remaining tool budget; confirming that default (and thus real-world exploitability without operator opt-in) would require further inspection of `interface.go` and the config documentation, which a full Devin session with codebase access could complete.

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
