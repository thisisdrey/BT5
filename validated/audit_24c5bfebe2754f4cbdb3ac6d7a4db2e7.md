## Title
Replay-window check/update TOCTOU permits duplicate packet delivery across concurrent listener routines - (File: `connection_state.go`)

### Summary
The RAAC report describes a class of bug where a protective state transition (closing/finalizing a liquidation) is decoupled from the action that should trigger it (repay), and the two steps can complete independently, leaving the system in an inconsistent state that an attacker (or normal user) can exploit before the "closing" step lands. In Nebula's data plane, the anti-replay mechanism has the same "check now, close later" structure: `ConnectionState.Decrypt` calls `window.Check` (the guard), performs the expensive decrypt operation *outside the lock*, and only afterward calls `window.Update` (the step that actually "closes" the window for that counter) under a freshly re-acquired lock.

### Finding Description
`ConnectionState.Decrypt` is structured as:
1. Lock, `cs.window.Check(l, messageCounter)` (read-only membership test), unlock.
2. Perform `cs.dKey.DecryptDanger(...)` — the expensive AEAD operation — with no lock held.
3. Lock, `cs.window.Update(l, messageCounter)` (the step that actually marks the counter as seen), unlock. [1](#0-0) 

Nebula can run multiple UDP reader routines that call into the same `Interface` (`listen.routines`/`tun.routines`, referenced in the changelog "Nebula can now do work on more than 2 cpu cores in send and receive paths via the new `routines` config option"), and `readOutsidePackets` looks up the `HostInfo`/`ConnectionState` by remote index and calls `hostinfo.ConnectionState.Decrypt` directly from whichever routine received the UDP datagram [2](#0-1) . Because `Check` and `Update` are two separate, individually-locked operations rather than one atomic check-and-set, an attacker who captures a single legitimate ciphertext packet and replays it multiple times in quick succession (a pure on-path/off-path replay, requiring no valid certificate or key material) can race multiple copies of the *same* packet through separate reader routines: both copies can pass `Check` before either has called `Update`, both get decrypted (an AEAD ciphertext decrypts deterministically to the same plaintext regardless of who asks), and only after decryption does the second copy's `Update` call fail as a duplicate — by which point the plaintext has already been handed to `handleOutsideMessagePacket`/the firewall/tun device.

This mirrors the RAAC pattern precisely: the "protective close" (`window.Update`, analogous to `closeLiquidation`/`finalizeLiquidation`) is not atomic with the "guard check" (`window.Check`, analogous to the liquidation-in-progress check in `repay`), so an action that should be prevented by the guard can complete before the closing step lands, permitting the once-guarded operation to succeed more than once.

The project's own history shows a directly related instance of this bug class already being patched in the relay path: “Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them” (#1751) and “Lock replay window updates so concurrent readers can't corrupt it” (#1802) [3](#0-2) . Those fixes addressed corruption/re-forwarding at the relay hop, but the terminal-endpoint `Decrypt` path still splits `Check` and `Update` across two separate lock acquisitions with the costly decrypt sandwiched in between, leaving the same class of race reachable at the tunnel endpoint under multi-routine configurations.

### Impact Explanation
An attacker who can observe/capture a single ciphertext packet on the wire (no valid certificate required — this is a passive/replay capability against already-encrypted traffic) can, under multi-routine UDP receive configurations, cause the same encrypted application-data packet to be decrypted and delivered to the tun device / firewall more than once despite the replay window nominally rejecting duplicates. This is a concrete violation of the anti-replay guarantee that `ReplayWindow`/`Bits` is meant to provide, and can duplicate injected packets into the protected network (e.g., duplicate transactions, duplicate control-plane messages) even though the design intends each message counter to be accepted exactly once.

### Likelihood Explanation
Exploitation requires: (a) `listen.routines`/multiple UDP reader goroutines processing the same tunnel concurrently, and (b) the attacker's ability to capture and quickly re-inject a legitimate ciphertext (a standard on-path replay primitive that needs no cryptographic material). The race window is narrow (bounded by AEAD decrypt time) but not implausible on a loaded host with several receive routines processing bursts of duplicated UDP datagrams targeting the same remote index concurrently.

### Recommendation
Make the replay-window admission atomic with respect to decryption: hold `decryptLock` across `Check` and either eagerly reserve the counter (tentatively mark seen) before decrypting, rolling back on decrypt failure, or restructure `Bits` to expose a single `CheckAndReserve`/`TestAndSet` operation that is called once under one critical section spanning the full check step, with `Update` only finalizing bookkeeping (not being the sole gate against duplicate delivery). This removes the TOCTOU window between "may proceed" and "is now marked seen."

### Proof of Concept
Conceptual PoC (not executed, no filesystem/tool access to run the live e2e suite in this session):
1. Configure two Nebula nodes with `listen.routines` > 1 to enable concurrent inbound UDP processing.
2. Establish a tunnel and capture one legitimate encrypted `header.Message` packet in flight (e.g., via a MITM/tap or `InjectUDPPacket`-style harness as used in `e2e/tunnels_test.go`'s `TestRelayReplayProtection`) [4](#0-3) .
3. Re-inject multiple copies of the exact same UDP packet in rapid succession so they land on different reader routines before either completes its `Decrypt` call.
4. Observe on the receiving tun device whether the same application packet is delivered more than once, which would demonstrate that `window.Update`'s duplicate rejection did not take effect atomically with `window.Check`.

Note: I was not able to execute this PoC or confirm the exact concurrency model of `listen.routines` reader dispatch to `readOutsidePackets` within this session's tool budget; this should be validated with a live multi-routine e2e test before treating it as fully confirmed.

### Citations

**File:** connection_state.go (L61-82)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}

	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	return out, nil
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

**File:** CHANGELOG.md (L79-81)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
```

**File:** e2e/tunnels_test.go (L377-382)
```go
// TestRelayReplayProtection asserts that a relay (forwarding-type) node rejects
// replayed relay frames. A captured relay frame, re-injected with the same
// message counter, must be dropped by the replay window rather than re-forwarded
// to the relay target. Before the fix, handleOutsideRelayPacket authenticated the
// frame but never advanced the replay window, so every replay was re-forwarded.
func TestRelayReplayProtection(t *testing.T) {
```
