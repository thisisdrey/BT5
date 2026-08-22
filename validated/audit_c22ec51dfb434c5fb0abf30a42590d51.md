Confirmed: `f.run()` in `interface.go` launches multiple concurrent `listenOut` goroutines (one per configured `routines`/queue), each independently calling `readOutsidePackets` → `hostinfo.ConnectionState.Decrypt`/`VerifyRelay` for packets that hash/route to the same `HostInfo`/`ConnectionState`. This confirms genuine concurrent access to a single `ConnectionState.window` from multiple reader routines, which is the precondition needed for the TOCTOU race in `Decrypt`/`VerifyRelay`. [1](#0-0) [2](#0-1) 

### Title
Replay-window Check/Update split allows duplicate packet double-acceptance in `ConnectionState.Decrypt`/`VerifyRelay` - (File: connection_state.go)

### Summary
`ConnectionState.Decrypt` and `ConnectionState.VerifyRelay` decide whether an incoming message counter is fresh by calling `window.Check()` under `decryptLock`, then **releasing the lock**, performing the (expensive) AEAD decryption, and only afterward re-acquiring the lock to call `window.Update()` to actually mark the counter as consumed. This mirrors the reported bug class: a value/decision (`_shares`/`_assets`, here "is this counter unseen") is computed from state that is mutated only later, in a separate step, after the lock protecting that state has been dropped and reacquired.

### Finding Description
In `connection_state.go`:

```go
func (cs *ConnectionState) Decrypt(...) ([]byte, error) {
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()          // <-- lock released
	...
	out, err = cs.dKey.DecryptDanger(...)   // <-- expensive, unlocked
	...
	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)  // <-- state mutated here
	cs.decryptLock.Unlock()
	...
}
```

The same pattern exists in `VerifyRelay`. Because nebula's `Interface.run()` starts one `listenOut` goroutine per configured queue/`routines` value, and each independently dispatches inbound UDP datagrams to `readOutsidePackets` → `Decrypt`/`VerifyRelay` for whatever `HostInfo` the packet's `RemoteIndex` resolves to, two goroutines can concurrently process two copies of the exact same on-wire packet (an attacker-supplied or network-duplicated replay of a previously captured message) that share the same `messageCounter`, addressed to the same tunnel. Both goroutines can call `window.Check()` before either calls `window.Update()`, since the lock is dropped between the two calls. Both `Check()` calls observe the counter as "not yet seen" and return `true`, so both packets pass the anti-replay gate and are decrypted and delivered to the tun device (or, in `VerifyRelay`, both are treated as authentic and the relay frame is forwarded a second time). This defeats the purpose of the replay window, whose entire job is to guarantee at-most-once acceptance per counter.

This is directly analogous to the reported ERC4626 bug: a value is computed from state (`convertToShares`/`convertToAssets`) before a later step actually mutates that state (`_processRewardsToPodLp`), so the computed value doesn't reflect the state as of when it takes effect. Here, "is this counter available" is decided from `window` state before the mutating `Update()` call, and an unprotected gap between the two lets a second, identical packet slip through using the same stale "not yet seen" answer.

### Impact Explanation
An attacker who can capture or otherwise obtain a single genuine encrypted data-plane message (no valid Nebula certificate needed — this only requires network-level visibility of UDP traffic between two Nebula hosts, e.g. via passive capture or by being on-path) can resend that exact UDP datagram back to the target listener. If the two copies land on different reader routines/queues (which nebula explicitly supports and encourages via `routines`/`listen.routines` for performance) and race through `Decrypt`, both are treated as legitimate and delivered to the tun device or, for relay frames, forwarded a second time by the relay. This breaks the replay-protection guarantee of the receive window (`Bits`), and for relayed traffic directly undermines the property asserted by `TestRelayReplayProtection` in `e2e/tunnels_test.go`, whose comment explicitly documents that failing to advance the replay window on the same goroutine step "re-forwarded every replay." The severity is bounded by requiring an actual race window and network conditions to line the two copies up close enough in time, and it does not by itself defeat AEAD confidentiality/integrity of new content — but it does allow at least one duplicate delivery/processing of tunnel-plane traffic per race, which for the relay path directly causes duplicate forwarding of attacker-replayed frames.

### Likelihood Explanation
Likelihood is medium: the race requires (1) `routines > 1` (multi-queue mode, which is a documented, encouraged performance configuration) so that `Decrypt`/`VerifyRelay` can genuinely execute concurrently for the same `ConnectionState`, and (2) the attacker being able to deliver two copies of the same captured packet closely enough in time that both hit `Check()` before either reaches `Update()`. Both conditions are realistic: multi-queue is a supported deployment mode, and duplicate delivery of a captured UDP datagram (double-send, or exploiting natural network/OS-level duplication) is trivial for a network-adjacent or on-path attacker.

### Recommendation
Hold `decryptLock` across the entire Check-decrypt-Update sequence (or otherwise make Check+Update atomic with respect to a given counter) so that no second caller can observe the "not yet seen" answer for a counter that is concurrently being consumed. E.g., check under the lock, decrypt (without holding the lock only if decryption is deterministic and failure doesn't need to release the slot), then re-verify with a single atomic check-and-mark operation (a `CheckAndUpdate` style call) protected the whole way through, so the decision and the mutation happen as one indivisible unit per counter.

### Proof of Concept
1. Configure a nebula host with `routines: 2` (or `listen.routines`/`tun.routines` > 1) so `Interface.run()` spawns multiple `listenOut` goroutines sharing the same `hostMap`/`ConnectionState` per peer, as seen in `interface.go` `run()`.
2. Establish a tunnel between two hosts and capture one legitimate encrypted data-plane UDP packet (`header.Message`) sent from peer A to peer B, noting its `MessageCounter`.
3. From an attacker-controlled vantage point with visibility to peer B's UDP listener, send two copies of the exact same captured datagram to peer B's listening socket in quick succession (e.g., over both queues if socket sharding via `SO_REUSEPORT` is used, or via simultaneous sends timed to race the recvmmsg batches across queues).
4. Observe (as in `e2e/tunnels_test.go`'s `TestRelayReplayProtection` methodology, adapted to the direct `Decrypt` path) that both copies are decrypted and delivered rather than the second being rejected with `ErrAlreadySeen`, because both goroutines executed `window.Check()` while `window.Update()` for the first copy had not yet run.

### Citations

**File:** interface.go (L273-287)
```go
func (f *Interface) run() {
	// Launch n queues to read packets from udp
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenOut(i)
		})
	}

	// Launch n queues to read packets from tun dev
	for i := 0; i < f.routines; i++ {
		f.wg.Go(func() {
			f.listenIn(f.readers[i], i)
		})
	}

```

**File:** outside.go (L309-326)
```go
	}

	version := int((data[0] >> 4) & 0x0f)
	switch version {
	case ipv4.Version:
		return parseV4(data, incoming, fp)
	case ipv6.Version:
		return parseV6(data, incoming, fp)
	}
	return ErrUnknownIPVersion
}

func parseV6(data []byte, incoming bool, fp *firewall.Packet) error {
	dataLen := len(data)
	if dataLen < ipv6.HeaderLen {
		return ErrIPv6PacketTooShort
	}

```
