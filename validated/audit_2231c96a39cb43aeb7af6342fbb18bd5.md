This is a real, well-known Nebula-class vulnerability class: `handleRecvError` acts on an unauthenticated `header.RecvError` packet, not on `UnmarshalPayload`/handshake decryption.

### Title
Unauthenticated `RecvError` packet tears down a live session without proof of key possession - (File: outside.go)

### Summary
Inbound `header.RecvError` packets are handled by `handleRecvError` in `outside.go` before any Noise decryption or `UnmarshalPayload` parsing occurs, based purely on the plaintext `h.RemoteIndex` field and the packet's source `netip.AddrPort`. An attacker who can send/spoof a UDP packet carrying a live session's remote index and matching the current source address can force `closeTunnel` and `handshakeManager.DeleteHostInfo` to run, tearing down an authenticated peer's tunnel with zero cryptographic proof of key possession.

### Finding Description
In `readOutsidePackets`, `header.RecvError` is dispatched straight to `f.handleRecvError(via.UdpAddr, h)` at [1](#0-0)  — this happens in the "Unencrypted packets" switch, before the hostmap lookup, before `Decrypt`, and long before any handshake `UnmarshalPayload` parsing would ever run.

`handleRecvError` then:
1. Checks a rate/accept policy (`acceptRecvErrorConfig.ShouldRecvError`) — a config gate, not authentication.
2. Looks up the hostinfo purely by the attacker-supplied plaintext index via `QueryReverseIndex(h.RemoteIndex)`.
3. Compares the *source address* of the packet to the hostinfo's cached remote address (`hr != addr`) as the only "spoofing" defense.
4. If they match, calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`. [2](#0-1) 

The only check preventing arbitrary teardown is a source-IP/port match against the *cached* remote — this is trivially satisfiable by an attacker who can spoof UDP source address/port (UDP has no return-routability proof), or simply by any off-path attacker who knows/guesses the victim's current external endpoint and the live session index (indices are only 32-bit and observable on the wire in the header of every packet exchanged with that peer, since `RemoteIndex` is sent in cleartext in every header, per `header.Encode` usage throughout `outside.go`, e.g. line 531). No AEAD tag, no Noise transcript, and no call into `handshake/payload.go`'s `UnmarshalPayload` is involved at all for this teardown path — it is a completely different, and weaker, code path than the actual handshake message flow (which does correctly gate `UnmarshalPayload` behind `m.hs.ReadMessage` success, per `handshake/machine.go` lines 228-241).

The question's framing (routes through `UnmarshalPayload`) does not match the actual reachable exploit path: `RecvError` never reaches `UnmarshalPayload`. The real reachable teardown path is `handleRecvError` directly, gated only by index-guessing and IP/port spoofing, not by any authenticated decryption.

### Impact Explanation
An unauthenticated attacker able to spoof a UDP source address/port matching the victim's cached remote (or an attacker who is on-path/NAT-shared with that address) can force teardown of any live Nebula tunnel by sending a single `RecvError` packet with the correct 32-bit remote index. This matches the "Denial of service tearing down another host's tunnel from an unprivileged network position" impact class — the session is destroyed and must be re-established via handshake, with no requirement that the attacker prove possession of session keys or a CA-signed certificate.

### Likelihood Explanation
Preconditions: the attacker needs (a) the live session's remote index (transmitted in the clear on every header on the wire, so observable by any on-path/eavesdropping party, or via traffic analysis), and (b) the ability to source a packet whose `via.UdpAddr` matches the hostinfo's currently cached remote (feasible if UDP source spoofing isn't blocked upstream, or if the attacker shares a NAT/observes the endpoint). This is not gated by CA certificates, host access, or leaked static keys — only by network-layer spoofability, which is realistic for UDP. It's a repeatable, single-packet DoS with no handshake required.

### Recommendation
Do not act on `RecvError` (or reduce its trust) without cryptographic proof: e.g., require the `RecvError` message itself to be authenticated (e.g., MAC'd with a key derived from the session, or embedded as an authenticated control message inside the encrypted tunnel rather than a bare unencrypted header type), or at minimum require a recent authenticated packet exchange window/challenge-response before honoring teardown from a `RecvError`. At the very least, treat `RecvError`-triggered teardown as advisory (e.g., only trigger a re-handshake attempt, not an unconditional `closeTunnel`+`DeleteHostInfo`), and rate-limit/require multiple corroborating unauthenticated signals before destructive action.

### Proof of Concept
Integration test plan (extending `e2e/tunnels_test.go` style harness):
1. Establish two real Nebula hosts A and B with a live encrypted tunnel (as in existing e2e tests).
2. Capture the plaintext `RemoteIndex` A uses for its session with B by parsing a real outbound header (`header.RemoteIndex` field is unencrypted on the wire).
3. From a third, unauthenticated UDP socket, craft a `header.RecvError` packet via `header.Encode(..., header.RecvError, 0, capturedIndex, 0)` and send it to A's UDP listen address, spoofing the source address/port to match B's cached remote (or, in a simplified test, directly invoke `f.handleRecvError(bAddr, h)` on A's `Interface` from the test).
4. Assert that before the packet, `A.hostMap.QueryVpnAddrsRelayFor`/`QueryReverseIndex` finds B's hostinfo; after the packet, assert the hostinfo has been deleted (`hostMap.QueryReverseIndex` returns nil) even though no `Decrypt` or Noise handshake message was ever accepted from an attacker holding B's or a CA-signed identity.
5. Expected (secure) behavior: the session should survive unless the `RecvError` is cryptographically authenticated; the test demonstrates it does not survive today.

### Citations

**File:** outside.go (L81-84)
```go
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
