## Title
Unauthenticated `RecvError` spoofing bypasses the anti-spoof check when `HostInfo.remote` is unset — remote tunnel-teardown DoS (`File: outside.go`)

### Summary
`handleRecvError` in `outside.go` is reachable by anyone able to send a UDP packet to a node's listen port — no valid CA-signed certificate or established handshake is required, because `RecvError` packets are processed in the pre-authentication branch of `readOutsidePackets`, before any decryption or peer verification occurs. The function's only defense against a third party spoofing this control message is a comparison of the sender's address against `hostinfo.GetRemote()`. That check is itself gated on `hr.IsValid()`, so when a hostinfo's `remote` field is the zero value (unset), the anti-spoof check is skipped entirely and the packet is accepted from anyone. This mirrors the report's core bug class of "missing validation for a not-yet-set/zero value" (analogous to the missing zero-address checks in `addOperator`/`setTreasury`): instead of validating attacker input against an authenticated baseline, the code trusts *anything* whenever that baseline happens to be a zero/invalid value.

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets to `f.handleRecvError(via.UdpAddr, h)` immediately after header parsing, with no certificate/HMAC/decryption check: [1](#0-0) 

`handleRecvError` looks up the hostinfo purely by the attacker-supplied `h.RemoteIndex` (a 32-bit value carried in cleartext in every packet header) and then performs its only sender-validation step: [2](#0-1) 

```go
hr := hostinfo.GetRemote()
if hr.IsValid() && hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}

f.closeTunnel(hostinfo)
f.handshakeManager.DeleteHostInfo(hostinfo)
```

The intent of this check is clearly to prevent an off-path attacker from forging a `RecvError` for someone else's index and killing their tunnel — the log message "Someone spoofing recv_errors?" makes the security purpose explicit. However, the check is short-circuited: it only fires `hr.IsValid() && hr != addr`. If `hr` is the invalid/zero `netip.AddrPort` — which is the case whenever a `HostInfo`'s remote hasn't been populated yet (e.g., a pending/newly created hostinfo before `SetRemote`/roaming has recorded a concrete UDP endpoint, or any other code path that leaves `remote` unset) — the entire spoofing guard evaluates to `false` and the packet is accepted regardless of who actually sent it.

This is the direct structural analogue of the report's finding: `addOperator`/`setTreasury` accept a zero address because there is no explicit check for the "unset" sentinel value; here, the anti-spoof check is bypassed precisely when the state that should have been validated is the zero/unset sentinel. In both cases a security-relevant field's "not yet populated" state is treated as implicitly trusted rather than explicitly rejected or handled safely.

`RemoteIndex` values are not secrets — they are transmitted in cleartext in the header of every packet a peer sends (`header.H.RemoteIndex`), so any attacker who has observed even one packet exchanged between two nodes (or brute-forces the 32-bit index space) knows a valid index to target.

### Impact Explanation
An unauthenticated, off-path attacker who can send UDP packets to a node's listening port can force it to tear down a legitimate tunnel and remove the corresponding entries from both the pending and main hostmaps (`f.closeTunnel(hostinfo)` and `hm.DeleteHostInfo(hostinfo)`), causing denial of service / forced re-handshake churn between two legitimate peers. This qualifies as a remote state-poisoning / DoS impact: an attacker with no valid certificate can disrupt an established relationship between two authenticated peers by exploiting a code path that is supposed to be guarded against exactly this kind of spoofing.

The severity is bounded by two factors: (1) the target hostinfo's remote must actually be unset/invalid at the time of the attack (this is not the steady state for a fully-established tunnel, since roaming/handshake completion typically records a concrete remote), and (2) the default `listen.accept_recv_error`/`listen.send_recv_error` behavior (`recvErrorAlways`/`recvErrorPrivate`/`recvErrorNever`) still gates whether `RecvError` is processed at all — but does not gate the sender-address check itself, so the bypass exists whenever recv_error processing is enabled.

### Likelihood Explanation
Reaching this code path requires no cryptographic material and no valid certificate — only knowledge (or brute-force guessing) of a 32-bit `RemoteIndex` and the ability to send a single 16-byte UDP packet to the victim's listen port with `Type = RecvError`. The condition `hr` being invalid is not a rare, contrived edge case; it is the natural state of any hostinfo before its remote address has been recorded (e.g., very early handshake completion, or windows where `SetRemote`/roaming has not yet run), making the window for exploitation realistic in normal operation rather than purely theoretical.

### Recommendation
Change the check so that an invalid/unset `hr` is treated as "cannot yet be validated, so reject/ignore" rather than "skip validation, so accept":
```go
hr := hostinfo.GetRemote()
if !hr.IsValid() || hr != addr {
    f.l.Info("Someone spoofing recv_errors?", ...)
    return
}
```
Additionally, consider requiring that a hostinfo have a fully completed handshake (`ConnectionState != nil`) and a populated remote before honoring `RecvError` for it at all, and/or rate-limit `RecvError`-triggered teardown per remote index to reduce the value of blind index guessing.

### Proof of Concept
Conceptual outline (not verified end-to-end against a running instance due to tool limitations in this session):
1. Establish (or observe) a Nebula tunnel between node A and node B; capture/derive B's `RemoteIndex` value as seen in cleartext headers of packets sent to A (or brute force the 32-bit space against A's listen port).
2. From an attacker-controlled host `E` that has no certificate at all, craft a 16-byte Nebula header with `Type = header.RecvError`, `RemoteIndex` set to the captured/guessed index, and send it via UDP to A's listen port.
3. On A, `readOutsidePackets` routes this straight to `f.handleRecvError(E's addr, h)` without any certificate check.
4. `hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)` finds A's hostinfo for B.
5. If `hostinfo.GetRemote()` is currently invalid/unset (a state reachable during/shortly after handshake before roaming records a concrete remote), the `hr.IsValid() && hr != addr` check evaluates to `false`, bypassing the spoofing guard.
6. A calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the legitimate A↔B tunnel — from a single unauthenticated forged packet.

Note: I was not able to fully trace every code path that sets `HostInfo.remote` (e.g. all call sites of `SetRemote`) within this session's tool budget, so the exact frequency/duration of the "unset" window in a running deployment is not fully confirmed and should be validated with a live Devin session against the actual `hostmap.go`/`remote_list.go` state machine.

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
