### Title
Unauthenticated single-packet `RecvError` teardown allows remote denial-of-service without a valid certificate - (File: `outside.go`)

### Summary
Nebula's `handleRecvError` accepts a bare, unencrypted, unauthenticated `header.RecvError` UDP packet and, in a single step with no confirmation or replay/challenge protection, tears down an already-established (authenticated) tunnel if the packet's source `netip.AddrPort` happens to match the hostinfo's currently recorded remote address. This mirrors the report's core defect class — a security-relevant state transition performed in one unauthenticated step instead of a verified, two-step (or otherwise confirmed) process — applied here to tunnel/session state rather than contract ownership.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets before any decryption or per-hostinfo authentication is performed: [1](#0-0) 

which routes straight into: [2](#0-1) 

The only gate checking that the packet's source address matches the last known remote (`hr != addr`) uses the plain UDP source `netip.AddrPort` from the packet, which is trivially spoofable — there is no MAC, no encryption, no nonce/sequence check, and no requirement that the sender hold a valid certificate. If an attacker can observe or guess the current `hr` (the peer's public-facing `ip:port`, often predictable/observable on the path or via other traffic), they can forge a single UDP packet with `header.RecvError` type and that source, causing:
- `f.closeTunnel(hostinfo)` — deleting the fully-authenticated hostinfo, and
- `f.handshakeManager.DeleteHostInfo(hostinfo)` — deleting pending state,

in one unauthenticated step, exactly analogous to the report's concern about a critical state-owning transition ("owner"/tunnel trust) being changed irrevocably without a second confirming step from a verified party.

This is reachable by an attacker who holds **no CA-signed certificate at all** — no handshake, no cryptographic material, is required to send this packet; it only requires knowledge/spoofing of the current UDP endpoint tuple, which fits the "no CA-signed certificate" reachability constraint (nonce/replay handling and firewall/session-teardown enforcement category).

### Impact Explanation
A successful forged `RecvError` immediately destroys an active, previously-authenticated tunnel between two legitimate Nebula peers, forcing a full re-handshake. Repeated forged packets constitute a persistent denial-of-service against specific overlay connections, degrading availability of the mesh. Because the action is irreversible and taken from a single unauthenticated packet (single-step trust action), it matches the acknowledged report pattern: critical, trust-affecting state changes should require a verified/two-step confirmation, not a bare unauthenticated signal.

### Likelihood Explanation
Likelihood is moderate: the attacker must know (or successfully guess/observe) the peer's currently active public `ip:port`. This is often learnable via network observation (the value is not secret on the wire) or via the lighthouse protocol disclosures, and the config knob `send_recv_error`/`accept_recv_error` (`recvErrorAlways` etc., see `interface.go`) may enable acceptance broadly (`recvErrorAlways`) depending on deployment, per the project's own CHANGELOG security note about `send_recv_error` exposing host presence. When enabled, exploitation requires only a single crafted, spoofed UDP packet — no cryptographic secrets, no completed handshake.

### Recommendation
Do not allow an unauthenticated, single unencrypted packet to unilaterally destroy already-established, cryptographically-verified tunnel state. At minimum:
- Require a MAC/HMAC keyed by session material (bound to `ConnectionState`) inside `RecvError` payloads before honoring them, rather than relying solely on source-address equality.
- Introduce a confirmation step (e.g., require repeated `RecvError` observations over a window, or require the acting side to attempt its own probe/re-handshake and only tear down after the peer fails to respond) rather than immediate one-shot teardown.
- Default `accept_recv_error`/`send_recv_error` to the most restrictive setting (`recvErrorNever` or `recvErrorPrivate`) and clearly document the DoS trade-off.

### Proof of Concept
1. Attacker observes (or otherwise learns) the current UDP `ip:port` (`hr`) that host A uses to reach host B for an established tunnel — no certificate or handshake needed.
2. Attacker crafts a single UDP packet with `header.RecvError` type/header (`header.Encode(..., header.RecvError, 0, index, 0)`), setting `RemoteIndex` to A's known/guessed local index for the tunnel with B, and sends it to A while spoofing the source address to equal `hr`.
3. On A, `readOutsidePackets` routes this straight to `handleRecvError` (`outside.go:82`) without decryption.
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, sees `hr == addr`, and immediately calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` (`outside.go:563-574`), tearing down the tunnel from a single unauthenticated, one-step packet.

Note: I was unable to fully confirm the default value of `accept_recv_error`/`send_recv_error` config knobs (`interface.go` had many matches but I could not read the exact default-setting lines before running out of tool calls) — this affects exploitability likelihood under default configuration and should be verified directly in `interface.go` (e.g., via a Devin session with full file access) before treating this as confirmed under default settings.

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
