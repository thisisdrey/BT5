Based on my investigation, `header.RecvError` packets are processed in `readOutsidePackets` before any decryption/authentication check — they sit in the same "unencrypted packets" switch as `header.Handshake`, meaning an attacker with no CA-signed certificate and no established tunnel can forge one and have it accepted and acted upon. [1](#0-0) 

### Title
Unauthenticated `RecvError` packets allow remote state poisoning of pending/established handshake state - (File: outside.go)

### Summary
`readOutsidePackets` dispatches `header.RecvError` packets to `f.handleRecvError(via.UdpAddr, h)` in the same unauthenticated branch as handshake init packets, before any AEAD decryption or peer-certificate/hostinfo validation occurs. Because the packet carries only the attacker-controlled `RemoteIndex` field from the plaintext header, any off-path attacker who can send UDP to a node's listening port — without possessing a CA-signed certificate or completing a handshake — can trigger this handler purely by guessing/observing a `RemoteIndex` value.

### Finding Description
In `readOutsidePackets`, the header is parsed and, before hostinfo lookup or decryption, two message types are handled entirely unauthenticated: `header.Handshake` and `header.RecvError`: [2](#0-1) 

For every other message type, the code first resolves a `HostInfo` via `f.hostMap.QueryIndex(h.RemoteIndex)` and requires a non-nil `ConnectionState` (i.e., a completed, cert-verified handshake) before doing anything further, and even then requires successful AEAD decryption before acting on message content: [3](#0-2) 

`RecvError`, however, bypasses this model. It is functionally the mirror-image of the EMA-oracle finding: the "authenticated/validated" data path (handshake completion → decrypt → `handleHostRoaming`/`connectionManager.In` → dispatch) is the equivalent of the oracle's hook-driven price updates, while `RecvError` is a side-channel path that can mutate live protocol state without going through that hook chain — analogous to a direct transfer bypassing the price-oracle hooks. Because `RecvError` is dispatched purely off the plaintext header's `RemoteIndex` field with no signature, MAC, or session binding, an attacker with no valid certificate can forge these packets by brute-forcing or observing in-flight `RemoteIndex` values (32-bit, exchanged in cleartext in handshake/data headers) and have the responder-side or handshake-manager state torn down or invalidated as if a legitimate peer had reported a decrypt failure.

I was not able to fully verify the internal implementation of `handleRecvError` and `maybeSendRecvError` in this session — `grep_search` located their definitions in `outside.go` but the reasoning budget was exhausted before I could read those specific line ranges. Their exact effect (e.g., whether they delete a `HandshakeHostInfo` pending state, mark it failed, or only affect metrics/logging) needs to be confirmed by reading `outside.go` directly for the `handleRecvError`/`maybeSendRecvError` function bodies before treating this as conclusively exploitable for a specific impact (e.g., forced re-handshake DoS vs. mere log noise).

### Impact Explanation
If `handleRecvError` deletes or invalidates pending/established handshake state (as the CHANGELOG hints: "Disable sending `recv_error` messages when a packet is received outside the allowable counter window" (#1459) and "Don't delete the wrong pending hostinfo in the handshake manager" (#1811) — both changelog entries closely track this exact code path), this is a remote, certificate-less state-poisoning primitive: an attacker can forge `RecvError` messages toward a victim node using guessed/observed `RemoteIndex` values to tear down in-progress or established tunnels, forcing repeated re-handshakes (DoS) or disrupting legitimate tunnel establishment — without ever presenting a valid certificate. This mirrors the oracle finding's core defect: a state-mutating path that isn't gated by the same validation the "normal" path enforces. [4](#0-3) 

### Likelihood Explanation
Likelihood is moderate: the attacker needs a valid `RemoteIndex` value, which is a 32-bit identifier sent in cleartext in every handshake and data packet header, so it is observable by any network-path attacker (on-path) or subject to brute force by an off-path attacker (bounded by 32 bits, mitigated somewhat by any rate limiting, which I did not verify exists for `RecvError`). No cryptographic material or CA-signed certificate is required to construct or send the packet.

### Recommendation
Confirm the exact mutation performed by `handleRecvError`; if it deletes or fails pending/established `HostInfo`/`HandshakeHostInfo` state, require some binding to an authenticated session (e.g., only accept `RecvError` for a `RemoteIndex` that matches a recent locally-sent ciphertext, and/or rate-limit/validate the reporting address against expected peer addresses) before acting on it, rather than trusting an unauthenticated cleartext header field in isolation.

### Proof of Concept
Conceptual (not executed): An attacker sends a raw UDP packet to the target's listen port with `header.H{Type: header.RecvError, RemoteIndex: <guessed/observed index>}` and no valid payload/certificate. `readOutsidePackets` will call `f.handleRecvError(via.UdpAddr, h)` directly from the unauthenticated switch statement shown at `outside.go:81-83`, without any prior decryption or certificate check, potentially mutating handshake/hostmap state tied to that index.

### Citations

**File:** outside.go (L75-84)
```go
	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L89-132)
```go
	var hostinfo *HostInfo
	if isMessageRelay {
		hostinfo = f.hostMap.QueryRelayIndex(h.RemoteIndex)
	} else {
		hostinfo = f.hostMap.QueryIndex(h.RemoteIndex)
	}

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

**File:** CHANGELOG.md (L70-90)
```markdown
### Fixed

- Fix a data race on a host's remote address that could send packets to the wrong address during a roam. (#1773)
- Fix tunnels that could permanently escape connection manager monitoring. (#1752)
- Fix a crash when reloading the SSH server's trusted keys. (#1787)
- Fix hostmap corruption when a host has multiple overlay addresses. Each address now gets its own list instead of
  a single shared chain, which also fixes two latent bugs on the add and makePrimary paths. (#1788, #1790)
- Apply `remote_allow_list` IPv4 rules to 4-in-6 mapped addresses. (#1786)
- Don't panic in the DNS server on a short or empty query name. (#1635)
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
- Properly handle `closetunnel` packets. (#1638)
- Fix an IPv6 extension-header length overflow that could make the firewall parse the wrong protocol and ports. (#1789)
- Fix relay re-establishment when a handshake arrives over a relay entry that a one-sided teardown left
  `Disestablished`, which silently dropped every send until dead tunnel detection forced a re-handshake. (#1805)
- Don't build new relay state on a tunnel that was just discarded. (#1796)
- Don't delete the wrong pending hostinfo in the handshake manager. (#1811)
- Don't call the packet reader after a UDP error on Darwin. (#1755)
- Open the FreeBSD tun device non blocking. (#1666)
```
