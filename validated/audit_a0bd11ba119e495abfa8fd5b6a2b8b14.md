### Title
Race window between replay-window `Check` and `Update` lets a duplicate/replayed data packet be decrypted and delivered twice - (File: `connection_state.go`)

### Summary
The reported Goldilocks bug is a classic check-then-act reentrancy: `redeemYield()` reads `yt.totalSupply()`/`yieldToken.balanceOf()`, mutates state (`burnYT`), and only *then* performs the external transfer — and a hook during that transfer lets the attacker re-enter and read the stale, not-yet-updated state to redeem more than they should. The reachable analog in this codebase is `ConnectionState.Decrypt` in [1](#0-0) , which checks the replay window, performs the (comparatively expensive) AEAD decryption *outside* the lock, and only afterward calls `Update` to actually mark the counter as consumed. Because Nebula supports multiple concurrent UDP reader routines (`SO_REUSEPORT` + `routines > 1`), two goroutines can race through `Check` for the same message counter before either one reaches `Update`, letting an attacker's replayed/duplicated packet be decrypted and delivered to the tunnel twice instead of being rejected as a duplicate.

### Finding Description
`Decrypt` is structured as:
1. Lock, call `cs.window.Check(l, messageCounter)`, unlock.
2. If `Check` returned true, perform `cs.dKey.DecryptDanger(...)` **without holding `decryptLock`**.
3. Lock again, call `cs.window.Update(l, messageCounter)`, unlock, and only reject as `ErrAlreadySeen` if `Update` says so. [1](#0-0) 

`Bits.Check` is a pure read — it does not mark the counter as seen; only `Bits.Update` mutates the bitmap and returns `false` for a duplicate. [2](#0-1) [3](#0-2) 

This mirrors the report's root cause exactly: state that gates the security decision (`yt.totalSupply`/`yieldToken.balanceOf`, here: "has this counter been marked seen") is read early and only committed late, with an intervening step (the external `safeTransfer` call in the report; the expensive `DecryptDanger` call here) that gives another concurrent path a window to observe the stale state and pass the same check.

Nebula is not single-threaded: `Main` creates one UDP listener per `routines` value with `SO_REUSEPORT` [4](#0-3) , and `Interface.run` launches one `listenOut` goroutine per routine, each independently invoking the packet-processing pipeline for whatever the kernel routes to that socket [5](#0-4) [6](#0-5) . The `decryptLock` in `ConnectionState` is explicitly `sync.Mutex`, showing the authors anticipated concurrent access to the same `ConnectionState`/replay window from multiple reader routines [7](#0-6) .

An attacker with no valid handshake/certificate can still capture and retransmit ("replay") an already-observed ciphertext packet toward a victim tunnel — the CHANGELOG itself documents that a very similar bug was previously fixed for relay frames ("Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them" and "Lock replay window updates so concurrent readers can't corrupt it") [8](#0-7) , and the project added an explicit regression test (`TestRelayReplayProtection`) for the relay path asserting a relay must drop duplicated frames [9](#0-8) . That fix, however, targeted the relay's `VerifyRelay` usage; the direct data-plane `Decrypt` path still has the same "Check outside the critical section that performs the expensive/external operation, Update afterward" shape, so the same class of bug — the replay window failing to actually block a replay when two evaluations of the same counter race — is reachable on the main tunnel decryption path whenever `routines > 1` is configured (a supported, common multi-queue configuration on Linux/BSD).

### Impact Explanation
If two copies of the same replayed ciphertext packet (with the same Noise message counter) are delivered to two different reader routines' sockets at close to the same time (attacker sends the duplicate right after intercepting/replaying it, timed to land on both `SO_REUSEPORT` sockets, or via multiple UDP paths/relays feeding the same hostinfo), both goroutines can pass `Check` before either calls `Update`. Both decryptions succeed and both plaintexts get delivered up the pipeline (into the tun device / firewall path) even though the replay window is supposed to guarantee each message counter is accepted at most once. This is a concrete traffic-replay/duplicate-delivery bypass of the anti-replay guarantee that Noise/Nebula's counter+bitmap window is meant to enforce, potentially causing duplicate application-layer packets to be injected into the tunnel (state poisoning at the transport layer) for any established tunnel — no valid CA-signed certificate is needed by the attacker performing the replay, since they are simply re-injecting an already-observed on-wire ciphertext.

### Likelihood Explanation
Requires: (1) `routines` configured > 1 (documented, supported multi-queue mode, not test-only), and (2) an attacker able to capture a legitimate packet and re-inject it twice in a tight enough window to hit two different reader goroutines before the first `Update` completes. This is a narrow, timing-dependent race rather than a deterministic bypass, so likelihood is low-to-medium, but it is remotely triggerable purely with network-level replay of already-seen ciphertext and requires no cryptographic material or valid credentials.

### Recommendation
Hold `decryptLock` for the entire `Check`-`Decrypt`-`Update` sequence (or otherwise atomically reserve the counter, e.g. an optimistic "claim" step similar to `Update`'s fast/slow path, done before decryption, with rollback on decryption failure) so that no two goroutines can simultaneously pass the check for the same counter. This mirrors the recommended fix in the report (`nonReentrant`/atomic guard around the whole check-mutate-external-call sequence) applied here as "atomic check-and-mark" around `Decrypt`.

### Proof of Concept
1. Configure a Nebula node with `routines: 2` (or more) so `SO_REUSEPORT` multi-queue UDP listening is active (`main.go:162-180`, `interface.go:243-288`).
2. Establish a tunnel to a target so a `ConnectionState` with an active replay window exists.
3. Capture one legitimate data-plane ciphertext packet (message counter `N`) sent to the target.
4. Rapidly re-inject two copies of the exact same captured packet at the target's UDP port, relying on kernel `SO_REUSEPORT` load-balancing to route them to two different `listenOut` reader goroutines that both invoke `f.readOutsidePackets` → `ConnectionState.Decrypt` concurrently.
5. Because `Check(l, N)` is evaluated under a lock that is released before the (comparatively slow) `DecryptDanger` call, both goroutines can observe `Check` returning true for counter `N` before either calls `Update`; both decryptions can succeed, and the duplicate/replayed plaintext can be delivered to the tun device twice, instead of the second copy being rejected with `ErrAlreadySeen`.

(Note: full confirmation of this race requires dynamic/concurrency testing — e.g. Devin running the existing `bits_test.go`/`connection_state_test.go` suite plus a targeted concurrent-`Decrypt` stress test — which was not possible to execute in this read-only analysis; the code-level TOCTOU shape and the multi-reader-routine reachability are established from the cited source, but empirical reproduction of the race timing window is unverified here.)

### Citations

**File:** connection_state.go (L17-27)
```go
type ConnectionState struct {
	eKey           noiseutil.CipherState
	dKey           noiseutil.CipherState
	myCert         cert.Certificate
	peerCert       *cert.CachedCertificate
	initiator      bool
	messageCounter atomic.Uint64
	window         *Bits
	decryptLock    sync.Mutex
	writeLock      sync.Mutex
}
```

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

**File:** bits.go (L134-150)
```go
// Check returns true if i is within (or way out in front of) the window, and not a replay
func (b *Bits) Check(l *slog.Logger, i uint64) bool {
	// If i is the next number, return true.
	if i > b.current {
		return true
	}

	if b.strictlyWithinWindow(i) {
		return !b.get(i)
	}

	// Not within the window
	if l.Enabled(context.Background(), slog.LevelDebug) {
		l.Debug("rejected a packet (top)", "current", b.current, "incoming", i)
	}
	return false
}
```

**File:** bits.go (L168-186)
```go
func (b *Bits) Update(l *slog.Logger, i uint64) bool {
	// Fast path: i is the next expected counter. Split out so the function
	// stays small and avoids paying for the slow paths' slog argument-build
	// stack frame on every call. The bit read/test/write is inlined to
	// touch the backing word once.
	if i == b.current+1 {
		pos := i & b.lengthMask
		word := pos >> 6
		mask := uint64(1) << (pos & 63)
		w := b.bits[word]
		if i > b.length && w&mask == 0 {
			b.lostCounter.Inc(1)
		}
		b.bits[word] = w | mask
		b.current = i
		return true
	}
	return b.updateSlow(l, i)
}
```

**File:** main.go (L162-180)
```go
		for i := 0; i < routines; i++ {
			l.Info("listening", "addr", netip.AddrPortFrom(listenHost, uint16(port)))
			udpServer, err := udp.NewListener(l, listenHost, port, routines > 1, c.GetInt("listen.batch", 64))
			if err != nil {
				return nil, util.NewContextualError("Failed to open udp listener", m{"queue": i}, err)
			}
			udpServer.ReloadConfig(c)
			udpConns[i] = udpServer

			// If port is dynamic, discover it before the next pass through the for loop
			// This way all routines will use the same port correctly
			if port == 0 {
				uPort, err := udpServer.LocalAddr()
				if err != nil {
					return nil, util.NewContextualError("Failed to get listening port", nil, err)
				}
				port = int(uPort.Port())
			}
		}
```

**File:** interface.go (L273-288)
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

}
```

**File:** interface.go (L309-337)
```go
func (f *Interface) listenOut(i int) {
	var li udp.Conn
	if i > 0 {
		li = f.writers[i]
	} else {
		li = f.outside
	}

	ctCache := firewall.NewConntrackCacheTicker(f.ctx, f.l, f.conntrackCacheTimeout)
	lhh := f.lightHouse.NewRequestHandler()
	plaintext := make([]byte, udp.MTU)
	h := &header.H{}
	fwPacket := &firewall.Packet{}
	nb := make([]byte, 12, 12)

	err := li.ListenOut(func(fromUdpAddr netip.AddrPort, payload []byte) {
		f.readOutsidePackets(ViaSender{UdpAddr: fromUdpAddr}, plaintext[:0], payload, h, fwPacket, lhh, nb, i, ctCache.Get())
	})

	// An error after teardown began is shutdown noise, the closed flag covers resources
	// Close releases itself and the cancelled ctx covers ones torn down by their owners
	// reacting to it, like the user device pipes
	if err != nil && !f.closed.Load() && f.ctx.Err() == nil {
		f.l.Error("Error while reading inbound packet, closing", "error", err)
		f.onFatal(err)
	}

	f.l.Debug("underlay reader is done", "reader", i)
}
```

**File:** CHANGELOG.md (L79-82)
```markdown
- Advance the replay window on relayed packets so a relay drops replayed frames instead of re-forwarding them. (#1751)
- Fix a race in relay state handling. (#1753)
- Lock replay window updates so concurrent readers can't corrupt it. (#1802)
- Reject malformed handshakes more reliably, including invalid ed25519 key lengths. (#1601, #1756)
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
