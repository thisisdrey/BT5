### Title
RIO ring-buffer slot is re-armed for a new kernel receive before its previous contents are read, allowing a racing UDP packet to splice a foreign address into another packet's payload - (File: udp/udp_rio_windows.go)

### Summary
`RIOConn.receive` dequeues a completed RIO receive, immediately calls `u.rx.Return(1)` and `u.insertReceiveRequest()` — which re-arms the *same* ring slot for a brand-new kernel write — and only afterwards reads `packet.addr` and copies `packet.data` out of that same slot's memory. Because Windows RIO is confirmed to be the default path on Windows (`NewListener` in `udp/udp_windows.go` tries `NewRIOListener` first), this is the live receive path, and the tight timing window between re-arming and reading lets an attacker who floods datagrams overwrite the slot's address/payload bytes before they are copied, producing an internally-inconsistent `(addr, data)` pair.

### Finding Description
`RIOConn.receive` (`udp/udp_rio_windows.go:184-254`) operates on a fixed-size ring of `packetsPerRing = 1024` pre-allocated `ringPacket{addr, data}` structs [1](#0-0) . At startup all 1024 slots are submitted as outstanding `ReceiveEx` requests [2](#0-1) , so the ring is always kept "full."

`ringBuffer.Push`/`Return` operate on `head`/`tail` counters modulo `packetsPerRing` [3](#0-2) . Because the ring starts saturated (`tail == head + packetsPerRing`), when `receive()` processes one completed packet at slot `head`, calling `u.rx.Return(1)` (advances `head` by 1) followed immediately by `u.insertReceiveRequest()` (which calls `Push()`, landing on `tail % packetsPerRing == head % packetsPerRing`, i.e. the **exact same slot that was just completed**) re-submits that same memory region to the kernel for a brand-new `WSARecvFrom`-style operation [4](#0-3) .

Only *after* this re-arming does the code dereference the completion's `RequestContext` to read the address and copy the payload out of that slot: [5](#0-4) 

Between the re-arm call and these two reads there is no synchronization preventing the kernel from writing a newly-arrived datagram's source address and payload into that same slot. If an attacker floods the listener with back-to-back UDP datagrams, a second real network completion can land in the just-recycled slot before line 251 (`ep := packet.addr`) or line 252 (`n := copy(buf, packet.data[...])`) execute, so the value returned by `receive()` can be an internally inconsistent splice: address bytes from one datagram and payload bytes from a different (earlier or later) datagram, or a partially-overwritten payload.

`ListenOut` immediately forwards whatever `receive()` returns to `readOutsidePackets` as if it were one atomic, trustworthy `(AddrPort, data)` pair [6](#0-5) . Nebula's higher layers (hostmap roaming detection, firewall's implicit source binding) rely on the assumption that the reported source address corresponds to the packet whose crypto/AEAD state was verified — this invariant is broken at the transport layer before any crypto check even happens.

### Impact Explanation
This breaks the assumption that the reported UDP source address always corresponds to the payload that was authenticated. A successfully-decrypting payload (e.g. a legitimate/replayable ciphertext the attacker can trigger, or simply their own valid session traffic) could be paired with an attacker-chosen foreign `AddrPort` that never sent that payload, if the race happens to land that way. This can feed a garbage/incorrect source address into `handleHostRoaming` and hostmap lookups, i.e. remote endpoint/state poisoning, and can affect firewall rules that key off the observed source address, potentially letting packets that should be denied by CIDR/group rules be associated with an address that passes those rules. This is a real state-poisoning primitive rooted in a memory-reuse-before-read bug, though it is Windows-RIO-specific.

### Likelihood Explanation
Preconditions: target must be running Windows with RIO available (this is the default receive path per `NewListener` in `udp/udp_windows.go`, falling back to generic sockets only if RIO init fails). No privileges, keys, or CA control are needed — the attacker only needs to send UDP datagrams fast enough to land a second kernel completion into the just-recycled slot before the Go code reads it out. This is a narrow, sub-microsecond timing window (a handful of instructions: `Return`, `insertReceiveRequest`'s pointer arithmetic and `ReceiveEx` syscall), so triggering it reliably requires sustained flooding and is probabilistic rather than deterministic, but it is repeatable given enough attacker-generated traffic and is not prevented by any existing check in the code (no versioning/generation counter is used on the ring slot).

### Recommendation
Read out `packet.addr` and copy `packet.data` into `buf` **before** calling `u.rx.Return(1)`/`u.insertReceiveRequest()`, so the slot is never re-armed for a new kernel write until its previous contents have been fully consumed. Alternatively, copy the completion's data into a local/stack buffer immediately upon dequeue, before doing any ring bookkeeping that could result in the same memory being reused.

### Proof of Concept
Add a build-tag-gated unit test for `udp_rio_windows.go` (or a race-detector-enabled fake winrio backend) that:
1. Mocks `winrio.DequeueCompletion` / `winrio.ReceiveEx` so completions can be driven deterministically and a "kernel write" can be injected between `u.rx.Return(1)`/`u.insertReceiveRequest()` and the subsequent reads.
2. Drives two synthetic completions: completion A carries `(addrA, dataA)`, completion B (simulating an attacker's second in-flight datagram) is written into the same physical ring slot right after `insertReceiveRequest()` re-arms it but before `receive()` reads `packet.addr`/`packet.data`.
3. Asserts that `receive()`'s returned `(n, ep)` and `buf[:n]` always correspond to a single completion's originally-submitted `(addr, data)` pair — i.e., `ep == addrA && bytes.Equal(buf[:n], dataA)` or `ep == addrB && bytes.Equal(buf[:n], dataB)`, but never a mix. Run with `go test -race` under heavy iteration count to demonstrate the mix occurs once the read-after-rearm ordering is used, and no longer occurs once the fix (read-before-rearm) is applied.

### Citations

**File:** udp/udp_rio_windows.go (L32-41)
```go
const (
	packetsPerRing = 1024
	bytesPerPacket = 2048 - 32
	receiveSpins   = 15
)

type ringPacket struct {
	addr windows.RawSockaddrInet6
	data [bytesPerPacket]byte
}
```

**File:** udp/udp_rio_windows.go (L75-80)
```go
	for i := 0; i < packetsPerRing; i++ {
		err = u.insertReceiveRequest()
		if err != nil {
			return nil, fmt.Errorf("init rx ring: %w", err)
		}
	}
```

**File:** udp/udp_rio_windows.go (L150-165)
```go
		n, rua, err := u.receive(buffer)

		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return err
			}
			// Dampen unexpected message warns to once per minute
			if lastRecvErr.IsZero() || time.Since(lastRecvErr) > time.Minute {
				lastRecvErr = time.Now()
				u.l.Warn("unexpected udp socket receive error", "error", err)
			}
			continue
		}

		r(netip.AddrPortFrom(netip.AddrFrom16(rua.Addr).Unmap(), (rua.Port>>8)|((rua.Port&0xff)<<8)), buffer[:n])
	}
```

**File:** udp/udp_rio_windows.go (L233-237)
```go
	u.rx.Return(1)
	err = u.insertReceiveRequest()
	if err != nil {
		return 0, windows.RawSockaddrInet6{}, err
	}
```

**File:** udp/udp_rio_windows.go (L250-253)
```go
	packet := (*ringPacket)(unsafe.Pointer(uintptr(results[0].RequestContext)))
	ep := packet.addr
	n := copy(buf, packet.data[:results[0].BytesTransferred])
	return n, ep, nil
```

**File:** udp/udp_rio_windows.go (L356-374)
```go
func (ring *ringBuffer) Push() *ringPacket {
	for ring.isFull {
		panic("ring is full")
	}
	ret := (*ringPacket)(unsafe.Pointer(ring.packets + (uintptr(ring.tail%packetsPerRing) * unsafe.Sizeof(ringPacket{}))))
	ring.tail += 1
	if ring.tail%packetsPerRing == ring.head%packetsPerRing {
		ring.isFull = true
	}
	return ret
}

func (ring *ringBuffer) Return(count uint32) {
	if ring.head%packetsPerRing == ring.tail%packetsPerRing && !ring.isFull {
		return
	}
	ring.head += count
	ring.isFull = false
}
```
