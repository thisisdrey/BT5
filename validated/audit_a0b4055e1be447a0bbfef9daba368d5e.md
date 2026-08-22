### Title
Unauthenticated `RecvError` packets allow any off-path attacker with a spoofed source address to force-close a legitimate victim's tunnel - (File: `outside.go`)

### Summary
This finding maps the H-27 bug class ("unrestricted, unauthenticated function that lets an attacker forcibly lock/disrupt a targeted legitimate user's state, front-running the legitimate flow") onto Nebula's `header.RecvError` handling path. `handleRecvError` in `outside.go` tears down an active tunnel and deletes its pending hostinfo entirely on the basis of an unauthenticated, unencrypted control packet whose only "protection" is a source-address equality check that is bypassable via UDP source-address spoofing.

### Finding Description
Incoming UDP packets of type `header.RecvError` are dispatched directly out of `readOutsidePackets` before any handshake, certificate, or decryption step: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely from the attacker-supplied `h.RemoteIndex` field (which is plaintext in every packet header, and thus observable by anyone who can see the victim's traffic, e.g. a network-level eavesdropper who never had to complete a CA-authenticated handshake), and only guards against forgery with a check that the sender's `addr` equals the currently known remote address of that hostinfo: [2](#0-1) 

Two conditions make this bypassable/abusable without ever holding a CA-signed certificate:
1. `hr.IsValid() && hr != addr` is the *only* anti-spoofing check. Any attacker who can spoof a UDP source `IP:port` matching the victim's real remote endpoint (a classic UDP spoofing technique, feasible on networks without egress/BCP38 filtering) satisfies this check trivially.
2. If `hr` is not yet valid (i.e., before the tunnel's remote endpoint has been pinned, `hostinfo.GetRemote()` returns an invalid `AddrPort`), the check is skipped entirely (`hr.IsValid()` is `false`), so the anti-spoofing branch is bypassed unconditionally and `f.closeTunnel(hostinfo)` runs with no address validation at all.

By default, `listen.accept_recv_error` is `"always"`, so `ShouldRecvError` unconditionally allows this: [3](#0-2) [4](#0-3) 

On a match, the manager tears down the tunnel and also deletes the pending hostmap entry to "allow for fast reconnect" - i.e. it actively destroys existing session state: [5](#0-4) 

This is directly analogous to `vestFor`: a function reachable with no authentication, keyed only by a target identifier (here `RemoteIndex` instead of a victim address), that unilaterally destroys/locks a legitimate user's active state (their tunnel), forcing them through a costly re-handshake cycle — the Nebula version of being "locked out."

### Impact Explanation
A successful attack forces `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` on a legitimate, already-established tunnel, causing denial of service: the victim's session is torn down and must re-handshake, and repeated abuse can indefinitely prevent a stable tunnel from being maintained between two legitimate peers — a remote state-poisoning/DoS impact achievable by a party with no CA-issued certificate.

### Likelihood Explanation
Likelihood is moderate: the attacker needs (a) the `RemoteIndex` value associated with the victim's tunnel — plaintext in every packet header and observable by any network-position attacker who can see victim traffic (no certificate needed to sniff UDP) — and (b) the ability to spoof a UDP source `IP:port` matching the current remote endpoint, or to race the window before `hr` becomes valid. UDP source spoofing is a long-standing, widely-documented network attack technique on networks lacking source-address validation (BCP38), so this is a credibly reachable scenario for a network-adjacent or spoofing-capable attacker without needing to complete any Nebula handshake or hold a trusted certificate.

### Recommendation
Do not rely solely on best-effort source-address comparison as the anti-spoofing control for a state-destroying action. Consider:
- Requiring `RecvError` messages to be authenticated (e.g., HMAC'd with the session key, or embedded as an authenticated in-tunnel control message rather than a bare unauthenticated header) so that only a party who has completed the handshake and possesses the derived keys can trigger a teardown.
- If backward compatibility prevents encrypting/authenticating `RecvError`, at minimum treat an "invalid/not-yet-set" `hr` as a signal to *not* skip the spoofing check, and add rate limiting / require multiple corroborating signals before tearing down an established (non-pending) tunnel.
- Consider defaulting `listen.accept_recv_error` to a stricter mode (e.g. `private`/`never`) rather than `always`.

### Proof of Concept
1. Attacker passively observes UDP traffic between victim A and victim B (e.g., on a shared/untrusted network segment) and extracts the plaintext `RemoteIndex` field from A's outgoing packet headers — no cert/handshake participation required.
2. Attacker crafts a `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, index, 0)`) with that `RemoteIndex`.
3. Attacker spoofs the UDP source address to match B's known remote `IP:port` as seen by A (or sends before A's `hostinfo.remote` is finalized, when `hr.IsValid()` is false), and sends it to A.
4. `f.handleRecvError` in `outside.go` finds the hostinfo via `QueryReverseIndex`, passes (or bypasses) the spoofing check, and calls `f.closeTunnel(hostinfo)` plus `hm.DeleteHostInfo(hostinfo)`, tearing down A's legitimate tunnel with B.

Note: I was unable to fully inspect `generateIndex`'s randomness properties or the exact `closeTunnel` implementation within the available index (only match locations were found, not full bodies), so the practical difficulty of guessing/observing a valid `RemoteIndex` in all deployment scenarios could not be fully confirmed from the indexed code alone.

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
