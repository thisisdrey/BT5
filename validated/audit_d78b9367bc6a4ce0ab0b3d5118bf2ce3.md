## Analysis

`handleRecvError` (`outside.go`) is the strongest reachable analog. It is invoked for the completely unauthenticated `header.RecvError` message type, before any handshake or decryption occurs [1](#0-0) . The function trusts the caller-supplied `h.RemoteIndex` to look up a hostinfo, checks only that the source UDP address optionally matches `hostinfo.GetRemote()` (a weak, spoofable IP-based check, not a cryptographic one), and if it matches (or the remote is not yet set), it immediately tears down the tunnel and deletes handshake state [2](#0-1) . This mirrors the report's bug class: a state-mutating function intended to be reachable only in a specific privileged/authenticated context (a genuine peer detecting an error) can instead be triggered by anyone on the network who can guess/observe a `RemoteIndex` and spoof a UDP source address — no certificate or handshake completion is required at all.

However, I could not fully verify how strong the existing IP-spoofing mitigation (`ShouldRecvError`/rate limiting in `acceptRecvErrorConfig`) is against a determined off-path attacker, since its implementation wasn't shown in the retrieved snippets. This is a real gap in my analysis — a complete assessment would require reading `recv_error` config handling code (rate limiter / allow-list logic) that wasn't retrieved.

### Title
Unauthenticated `RecvError` packets allow remote tunnel teardown via spoofed UDP source and guessed index - (File: outside.go)

### Summary
`handleRecvError` processes the `header.RecvError` message type without requiring any cryptographic authentication (handshake completion, AEAD verification, or certificate check). It is dispatched straight from `readOutsidePackets` before decryption [1](#0-0) , and its only "authentication" is comparing the UDP source address of the incoming packet to the tunnel's currently known remote address [3](#0-2) .

### Finding Description
Analogous to `receiveCollateral()` trusting caller-supplied state instead of verifying the caller's identity, `handleRecvError` trusts an attacker-supplied `RemoteIndex` header field and an easily spoofed source IP/port to determine whether to tear down an active, authenticated tunnel:

- The dispatch path in `readOutsidePackets` routes `header.RecvError` packets directly to `f.handleRecvError(via.UdpAddr, h)` before any packet content is decrypted or verified [1](#0-0) .
- `handleRecvError` looks up a hostinfo purely from the attacker-controlled `h.RemoteIndex` field via `QueryReverseIndex` [4](#0-3) .
- The only check is `hr.IsValid() && hr != addr` — if the attacker either doesn't yet know the current remote (spoofing an unset endpoint) or successfully spoofs the UDP source address matching the current remote, the check passes silently [3](#0-2) .
- On success, the function unconditionally calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, destroying live, mutually-authenticated tunnel state [5](#0-4) .

This is comparable to the reported bug: a state-mutating operation gated only by a caller-supplied value rather than genuine authentication of the calling party.

### Impact Explanation
An attacker with no CA-signed certificate — able only to send arbitrary UDP packets to a Nebula node's listening port — can forge a `RecvError` header with a spoofed source address and a `RemoteIndex` (either observed on the wire or brute-forced, since indexes are 32-bit but not secret in transit) to force termination of another host's established tunnel. This is a remote denial-of-service / state-poisoning primitive: it does not require possessing a valid certificate, completing a handshake, or being able to decrypt traffic — only the ability to spoof a UDP source and know/guess a 32-bit index value.

### Likelihood Explanation
Likelihood is moderate: UDP source spoofing is feasible on many networks, and `RemoteIndex` values are transmitted in cleartext headers on other packets an attacker may observe (e.g., via passive network position), making the index non-secret. The mitigating `ShouldRecvError`/`acceptRecvErrorConfig` gating (rate limiting or config option) reduces exploitability, but its concrete strength could not be verified from the code retrieved in this session.

### Recommendation
Do not tear down tunnels based solely on an unauthenticated `RecvError` header and a plain source-IP comparison. At minimum:
- Require the accompanying UDP source address to exactly match the actively-in-use remote (already partially done), and additionally rate-limit/backoff per remote index to prevent blind spoofing floods.
- Consider requiring an authenticated (encrypted/MACed) acknowledgment before acting on `RecvError`, or treat it purely as a hint to re-probe rather than an unconditional trigger for `closeTunnel`/`DeleteHostInfo`.
- Audit `acceptRecvErrorConfig.ShouldRecvError` (not available in this review) to confirm it provides adequate protection against spoofed sources; if it merely rate-limits, that is not equivalent to authentication.

### Proof of Concept
1. Observe or guess a victim host's `RemoteIndex` for an active tunnel (index appears in cleartext in every Nebula packet header sent to/from that host, so passive observation on the path or a MITM position reveals it).
2. Craft a UDP packet with `header.RecvError` type and the target `RemoteIndex`, spoofing the source address to match the current remote endpoint of the tunnel (or send it before the remote is set, when `hr.IsValid()` is false).
3. Send this bare packet (no valid certificate, no handshake) to the victim's Nebula listener.
4. `handleRecvError` passes the weak check at [3](#0-2)  and unconditionally calls `closeTunnel`/`DeleteHostInfo`, tearing down the legitimate tunnel [5](#0-4) .

### Citations

**File:** outside.go (L81-83)
```go
	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
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
