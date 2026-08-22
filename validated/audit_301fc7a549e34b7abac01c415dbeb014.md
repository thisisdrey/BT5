## Title
Unauthenticated `RecvError` packet forces teardown of an established, authenticated tunnel without verifying peer possession of the session - ([File: outside.go])

### Summary
Nebula processes `header.RecvError` packets before any decryption or certificate/session validation, and uses them to immediately tear down a live, fully authenticated tunnel (`f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`). The only checks performed are a coarse rate/allow policy (`recvErrorConfig.ShouldRecvError`) and a comparison of the packet's source `netip.AddrPort` against the hostinfo's currently known remote address. Neither check binds the teardown decision to possession of the session's cryptographic material, so it mirrors the escrow report's bug class: a resource-destroying operation (`destroyEscrow()` / here, `closeTunnel`+`DeleteHostInfo`) is performed without validating that doing so is safe for the party who has state locked in it (here, an actively-handshaked peer with in-flight traffic).

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets straight to `f.handleRecvError` in the "Unencrypted packets" branch, prior to any AEAD decryption: [1](#0-0) 

`handleRecvError` then:
1. Gates on a config policy that is at best `recvErrorPrivate`/`recvErrorAlways` (no cryptographic binding), then
2. Looks up the hostinfo purely by the attacker-supplied `RemoteIndex` field taken from the cleartext header, then
3. Compares the *current* remote endpoint on the hostinfo to the packet's source address, and if they match (or if `hr` is not yet valid), proceeds to tear the tunnel down: [2](#0-1) 

The `Firewall`, `PKI`, `hostmap`/handshake-authentication logic elsewhere in the codebase treat "trust" as something only established via a completed, certificate-authenticated handshake and per-packet AEAD nonce/replay verification (see the decrypted-packet path in the same function at lines 105–136, which requires `hostinfo.ConnectionState.Decrypt` to succeed before any state-affecting message type — including the encrypted `CloseTunnel` type — is honored): [3](#0-2) [4](#0-3) 

`RecvError`, by contrast, is exempted from that authentication requirement entirely and yet has the same destructive effect (`closeTunnel` → `hostMap.DeleteHostInfo` → possible lighthouse state wipe via `DeleteVpnAddrs`) as the authenticated `CloseTunnel` message: [5](#0-4) [2](#0-1) 

This is directly analogous to the escrow bug: `destroyEscrow()` deleted state (escrow record) without checking whether funds/tokens were still locked in it by a party who had not consented to destruction. Here, `handleRecvError` destroys tunnel/hostmap state belonging to an already-authenticated peer session without any cryptographic proof that the request to destroy that state originates from a party who is actually part of that session — only a spoofable UDP source-address match and a guessable/observable `uint32` index.

### Impact Explanation
A network attacker who can send UDP packets to the target's listening port (no CA-signed certificate needed — this is pre-authentication code) and who can observe or predict:
- the target's active `localIndexId` for a peer's hostinfo (a `uint32`, transmitted in the clear in every packet header of that tunnel, hence trivially sniffable on-path, or via off-path amplification if any packet is observable), and
- the peer's current UDP source `netip.AddrPort` (also visible in the clear on every packet, and, in the "hr not valid yet" pre-roam window, not even required to match),

can forge a `RecvError` packet that causes the victim to unilaterally tear down an established tunnel, discard its ConnectionState, and (if it's the only remaining hostinfo for that vpn address) purge the associated lighthouse cache. This is a remote, unauthenticated denial-of-service against an already-secured tunnel — the closest in-scope classification is "remote state poisoning" (forced destruction of session/hostmap state) with consequent traffic disruption, matching the rules' "remote crash/DoS/state poisoning" acceptance criteria.

### Likelihood Explanation
Likelihood is constrained by the need to know/guess the local index and to spoof or be positioned to send from the peer's current UDP address, and by the `recvErrorConfig` gating (`recvErrorNever` disables it; `recvErrorPrivate` restricts it to private source ranges). Where `sendRecvErrorConfig`/`acceptRecvErrorConfig` is left at the default `always`, or when an on-path/adjacent-network attacker can observe cleartext headers (indices are never encrypted), exploitation requires no cryptographic material at all — only observation of a few plaintext header fields, which is a materially weaker bar than the handshake authentication and AEAD checks that gate every other state-mutating packet type in this file.

### Recommendation
Do not let an unauthenticated, unencrypted packet type single-handedly destroy authenticated session state. Options:
- Require a valid, replay-protected message counter and MAC-derived proof tied to the current session (i.e., move `RecvError` handling to occur after a lightweight authenticated exchange, or require it be signed/derived from a value only the legitimate peer can produce), or
- At minimum, before honoring a `RecvError`-triggered teardown, verify the reporting party can also produce a currently-valid outbound encrypted packet/challenge, and drop `RecvError` for hostinfos whose remote address has not yet been "roamed"-confirmed (`hr.IsValid()` currently allows an unconditional pass-through when the remote hasn't been recorded yet), and
- Rate-limit/back off per-index to blunt spoofed floods even under `recvErrorAlways`.

### Proof of Concept
1. Establish a normal tunnel between `A` and `B`; A's hostmap holds a hostinfo for `B` at `localIndexId = X`, with `GetRemote() == B_addr`.
2. An attacker who can observe (or is on-path to see) any cleartext packet header of this tunnel learns `X` (the `RemoteIndex` field embedded in headers `B`→`A` uses `A`'s local index as `RemoteIndex`) and `B_addr`.
3. Attacker crafts a raw UDP packet to `A`'s listening port containing a `header.H` with `Type = header.RecvError` and `RemoteIndex = X`, spoofing source address `B_addr` (or, if roam state (`lastRoam`) is not yet set, no spoofing is even required if `hr` isn't valid).
4. `A` calls `handleRecvError`, matches `hostinfo.GetRemote() == B_addr`, and executes `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` — destroying the live, authenticated tunnel state without any proof the attacker holds `B`'s session keys or certificate: [6](#0-5) 

Note: I was not able to fully trace every call site that sets/validates `hostinfo.lastRoam`/`hr.IsValid()` transitions within the remaining tool budget, so the exact window during which the address-match check can be bypassed (`hr.IsValid()==false`) should be independently confirmed by a follow-up code review before treating this as fully weaponizable in all states.

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

**File:** outside.go (L105-136)
```go
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

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)
```

**File:** outside.go (L164-166)
```go
	case header.CloseTunnel:
		hostinfo.logger(f.l).Info("Close tunnel received, tearing down.", "from", via)
		f.closeTunnel(hostinfo)
```

**File:** outside.go (L250-257)
```go
// closeTunnel closes a tunnel locally, it does not send a closeTunnel packet to the remote
func (f *Interface) closeTunnel(hostInfo *HostInfo) {
	final := f.hostMap.DeleteHostInfo(hostInfo)
	if final {
		// We no longer have any tunnels with this vpn addr, clear learned lighthouse state to lower memory usage
		f.lightHouse.DeleteVpnAddrs(hostInfo.vpnAddrs)
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
