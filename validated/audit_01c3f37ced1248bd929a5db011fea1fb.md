### Title
Unauthenticated `recv_error` Tunnel Teardown DoS via Bypassable Remote-Address Equality Check - (File: outside.go)

### Summary
`Interface.handleRecvError` in `outside.go` tears down an existing tunnel based solely on an unencrypted, unauthenticated `RecvError` packet whose only binding to the real session is a 32-bit `RemoteIndex` value and a remote-address equality check that is skipped whenever the target hostinfo has no confirmed remote address yet. This mirrors the GMX `createDeposit` bug class: a strict equality guard (`hr != addr`) that is meant to prevent a third party from forging state is not always enforced, allowing an attacker without any CA-signed certificate to corrupt shared per-tunnel state and force a legitimate peer's tunnel to be destroyed.

### Finding Description
`RecvError` packets are handled in the fully unencrypted branch of `readOutsidePackets`, before any certificate or `ConnectionState` is consulted: [1](#0-0) 

`handleRecvError` looks the hostinfo up purely by the wire-visible `RemoteIndex` field and only rejects the packet if the hostinfo already has a *valid* recorded remote address that mismatches the sender: [2](#0-1) 

The guard `if hr.IsValid() && hr != addr` is a strict equality/validity check analogous to the GMX `executionFee` equality check: it is intended to stop an off-path/unauthenticated party from spoofing control over someone else's tunnel state. However, when `hr.IsValid()` is false (e.g., the hostinfo has not yet had its remote address confirmed — the same nil/zero state that exists briefly after a handshake, for relayed hosts, or for any hostinfo whose `SetRemote` has not yet fired), the equality check is bypassed entirely, and `closeTunnel` + `DeleteHostInfo` run unconditionally as soon as the attacker supplies a matching `RemoteIndex`.

`RemoteIndex` is not confidential: it is generated as a plain 32-bit random value (`generateIndex`) and transmitted in the header of handshake and data packets without encryption, so it is observable by anyone who can see the UDP traffic between two nebula nodes (no cert or PKI trust required): [3](#0-2) 

Whether or not `hr` has been set is entirely a function of the local host's tunnel state, which the attacker does not control and cannot verify before firing the spoofed `RecvError` — exactly the same "external, uncontrolled third-party state corrupts a strict comparison that a legitimate flow depends on" pattern as the GMX report, where a third party altering the WETH balance broke the `executionFee` equality check for someone else's deposit.

By default `recv_error` acceptance is unrestricted (`always`), so this path is reachable without any additional configuration: [4](#0-3) 

### Impact Explanation
An attacker who can observe (on-path, no cert) or brute-force a 32-bit `RemoteIndex` can send a single unauthenticated, unencrypted UDP packet that:
- Deletes the victim's active `HostInfo` from both the main and pending hostmaps (`closeTunnel`, `DeleteHostInfo`), tearing down an established, legitimately-authenticated tunnel.
- Forces the victim to re-handshake, causing service disruption / repeated connection resets — a remote state-poisoning and denial-of-service impact against the mesh, achievable without holding any CA-signed certificate.

### Likelihood Explanation
Exploitability depends on the attacker being able to learn a valid `RemoteIndex` (trivial for an on-path attacker who can see the cleartext handshake header, and non-trivial but not implausible via brute force/flooding of the 32-bit space against a busy lighthouse or gateway node) and the target hostinfo momentarily lacking a validated remote (`hr.IsValid()==false`), a state that legitimately occurs for pending/relayed hostinfos and immediately post-handshake before the address is confirmed. This is a narrower window than a fully unconditional bypass, but it requires no cryptographic material and no packet decryption, distinguishing it from all other authenticated data-plane paths in the codebase.

### Recommendation
- Require that the `hr` (remote) comparison be authoritative even when `IsValid()` is false — e.g., drop `RecvError` for any hostinfo that has not yet completed a mutually-authenticated handshake with a confirmed remote, rather than silently allowing teardown.
- Consider strengthening `RecvError` binding beyond the bare `RemoteIndex`/address equality (e.g., requiring it be AEAD-protected/signed with the session key, similar to other post-handshake control messages) so a passive/off-path observer of the unencrypted index cannot spoof it.
- Continue to default `accept_recv_error` toward the most restrictive practical setting for untrusted networks (mirrors the fix intent in `#1569`/`#1459` from the CHANGELOG).

### Proof of Concept
1. Attacker sniffs (or brute-forces) the `RemoteIndex` used between victim node A and node B — visible in cleartext in the handshake/data header per `header.Encode`/`generateIndex`.
2. Before node B's hostinfo for A has a validated remote (`hr.IsValid()==false` — e.g., right after a relay-established or not-yet-roamed session), attacker crafts a bare `RecvError` header packet:
   `header.Encode(buf, header.Version, header.RecvError, 0, <observed RemoteIndex>, 0)`
   and sends it to node B's UDP listener from an arbitrary source address.
3. `readOutsidePackets` routes this to `handleRecvError` (outside.go:81-83), which finds the hostinfo via `QueryReverseIndex`, sees `hr.IsValid()==false`, skips the address check, and calls `f.closeTunnel(hostinfo)` plus `handshakeManager.DeleteHostInfo(hostinfo)` (outside.go:557-575), destroying B's tunnel state for A without any certificate or session key.

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

**File:** handshake_manager.go (L624-643)
```go
func generateIndex(l *slog.Logger) (uint32, error) {
	b := make([]byte, 4)

	// Let zero mean we don't know the ID, so don't generate zero
	var index uint32
	for index == 0 {
		_, err := rand.Read(b)
		if err != nil {
			l.Error("Failed to generate index", "error", err)
			return 0, err
		}

		index = binary.BigEndian.Uint32(b)
	}

	if l.Enabled(context.Background(), slog.LevelDebug) {
		l.Debug("Generated index", "index", index)
	}
	return index, nil
}
```

**File:** CHANGELOG.md (L128-131)
```markdown
### Added

- Add a config option to control accepting `recv_error` packets which defaults to `always`. (#1569)

```
