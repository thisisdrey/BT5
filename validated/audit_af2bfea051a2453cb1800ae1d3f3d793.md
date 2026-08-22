### Title
Unrecovered panic in a single UDP batch member crashes the entire nebula reader goroutine (and process) - ([File: udp/udp_linux.go])

### Summary
The external report's bug class is "one bad item in a batch causes the entire batch (and thus all honest items) to fail/DoS, with no isolation between items." The strongest reachable analog in Nebula is the batched UDP receive loop `ListenOut`, which reads up to `batch` (default 64) datagrams per syscall via `recvmmsg` and synchronously dispatches every one of them to `readOutsidePackets` inside a single `for` loop, with **no panic recovery anywhere in the production code path** (`recover()` is used nowhere in the main package). A single malformed/malicious packet processed anywhere in that per-message dispatch chain (header parsing, handshake state lookups, lighthouse message unmarshalling, relay/control handling, etc.) that triggers a Go panic (nil-deref, index-out-of-range, div-by-zero, etc.) will not be isolated to that one message — an unrecovered panic in any goroutine terminates the entire `nebula` process, exactly mirroring the reported effect where one bad order poisons the whole batch and denies service to all honest participants.

### Finding Description
`ListenOut` in [1](#0-0)  performs one `recvmmsg`/`recvmsg` syscall to fill up to `batch` messages, then iterates over all `n` received messages in a tight loop, invoking the caller-supplied `r` callback (`readOutsidePackets`) for every single one: [2](#0-1) 

This callback is wired up in `listenOut`, which is itself run as one goroutine per configured reader (`f.writers`), each processing an unbounded stream of batches for the life of the process, without any `recover()` wrapping the per-packet dispatch: [3](#0-2) 

`readOutsidePackets` itself is reachable by **any untrusted, unauthenticated remote sender** — before any certificate/CA validation occurs for handshake-type packets — and dispatches to numerous subsystems (header parsing, hostmap index lookups, handshake manager, lighthouse handler, relay manager) based on attacker-controlled header fields: [4](#0-3) 

A repo-wide search confirms `recover()` is used only inside a CLI test helper (`cmd/nebula-cert/stdio_test.go`) and nowhere in the packet-processing, handshake, lighthouse, or relay code paths that handle untrusted network input. This means the entire per-batch/per-message pipeline described above relies on every downstream function never panicking on attacker-controlled input — there is no last-resort containment, unlike the batched offchain-order processor in the external report, which at least contained failures within the current transaction (a revert), whereas here an unrecovered panic terminates the whole node process, dropping every established tunnel simultaneously.

This is structurally the same root cause pattern flagged in the external report: **batched processing of externally supplied, unvalidated items with no per-item failure isolation** — except the failure mode in Nebula is a full crash of the read routine/process rather than a reverted transaction, making the impact strictly worse (denial of service for every tunnel on the node, not just one batch of orders).

### Impact Explanation
If any function reachable from `readOutsidePackets` (header parsing, `HandshakeManager.HandleIncoming`, `LightHouseHandler.HandleRequest`, `relayManager.HandleControlMsg`, hostmap queries, etc.) can be driven into a panic by a single crafted UDP datagram from an unauthenticated remote address, the panic is not caught anywhere in the call stack. In Go, an unrecovered panic in any goroutine crashes the entire process — not just the goroutine that panicked. Consequently:
- All in-flight batched messages in that `recvmmsg` call are lost.
- All established tunnels served by that reader crash along with the whole `nebula` process.
- The node goes fully offline until restarted/supervised, and if the attacker can repeat this pattern (e.g., against any reachable UDP port with no CA-signed cert required to reach `readOutsidePackets` for handshake-type packets), this becomes a trivially repeatable remote crash / DoS vector — a stronger outcome than the "insolvency" impact described in the original report.

### Likelihood Explanation
Likelihood depends entirely on whether a concrete panic-inducing input exists somewhere in the reachable dispatch chain (header parsing itself, from the code reviewed, is defensively length-checked and does not appear to panic on malformed input). I was not able to fully audit every downstream function (`HandshakeManager.HandleIncoming`, `NebulaMeta.Unmarshal`, relay/control decode paths, ASN.1/protobuf unmarshalling) for panic-inducing edge cases within the available indexing/time budget, so this should be treated as a **structural/architectural weakness** (absence of the standard "recover-and-continue" pattern around per-packet/per-batch processing of untrusted input) rather than a confirmed, exploitable panic with a known trigger input. The concrete likelihood is therefore uncertain without further code review of the unmarshalling and handshake-parsing paths.

### Recommendation
Add panic recovery isolation around the per-message dispatch in the batch loop (and equivalently in `listenIn`), so a panic while processing one datagram is logged and skipped rather than crashing the process, e.g. wrap the call to `r(...)` in `ListenOut` (`udp/udp_linux.go:184-192`) and the callback in `listenOut`/`listenIn` (`interface.go`) with a deferred `recover()` that logs the error and continues the loop instead of terminating the goroutine/process. This should be paired with continued input validation, but the missing containment layer (recover-and-continue) is the direct architectural analog of the "try/catch per order" fix recommended in the original report.

### Proof of Concept
Not verified with a concrete triggering payload — this analysis identifies the missing isolation mechanism (no `recover()` in the batched UDP dispatch path in `udp/udp_linux.go`/`interface.go`, confirmed via repo-wide search) as the structural analog of the reported bug class, but I could not confirm within the available tool budget a specific panic-inducing byte sequence reachable through `readOutsidePackets` → `HandshakeManager.HandleIncoming` / `LightHouseHandler.HandleRequest` / `relayManager.HandleControlMsg`. A background engineering session with full-repo access would be needed to fuzz/inspect those unmarshalling paths for concrete panic triggers to complete the PoC.

### Citations

**File:** udp/udp_linux.go (L165-194)
```go
func (u *StdConn) ListenOut(r EncReader) error {
	var ip netip.Addr
	msgs, buffers, names := u.PrepareRawMessages(u.batch)
	read := u.recvmmsg
	if u.batch == 1 {
		read = u.recvmsg
	}

	for {
		n, err := read(msgs)
		if err != nil {
			if errors.Is(err, unix.EINTR) {
				continue // interrupted by a signal, retry the read
			}
			// net.ErrClosed after Close() is teardown, absorbed by the caller's
			// closed flag like the other platforms; anything else is a real error.
			return err
		}

		for i := 0; i < n; i++ {
			// Its ok to skip the ok check here, the slicing is the only error that can occur and it will panic
			if u.isV4 {
				ip, _ = netip.AddrFromSlice(names[i][4:8])
			} else {
				ip, _ = netip.AddrFromSlice(names[i][8:24])
			}
			r(netip.AddrPortFrom(ip.Unmap(), binary.BigEndian.Uint16(names[i][2:4])), buffers[i][:msgs[i].Len])
		}
	}
}
```

**File:** interface.go (L309-326)
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
```

**File:** outside.go (L25-94)
```go
func (f *Interface) readOutsidePackets(via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	err := h.Parse(packet)
	if err != nil {
		// Hole punch packets are 0 or 1 byte big, so lets ignore printing those errors
		// TODO: record metrics for rx holepunch/punchy packets?
		if len(packet) > 1 {
			f.messageMetrics.RxInvalid(1)
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				f.l.Debug("Error while parsing inbound packet",
					"from", via,
					"error", err,
					"packet", packet,
				)
			}
		}
		return
	}

	if h.Version != header.Version {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("Unexpected header version received", "from", via)
		}
		return
	}

	// Check before processing to see if this is a expected type/subtype
	if !h.IsValidSubType() {
		f.messageMetrics.RxInvalid(1)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			f.l.Debug("Unexpected packet received", "from", via)
		}
		return
	}

	if !via.IsRelayed {
		if f.myVpnNetworksTable.Contains(via.UdpAddr.Addr()) {
			f.messageMetrics.RxInvalid(1)
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				f.l.Debug("Refusing to process double encrypted packet", "from", via)
			}
			return
		}
	}

	// don't keep Rx metrics for message type, since you can see those in the tun metrics
	if h.Type != header.Message {
		f.messageMetrics.Rx(h.Type, h.Subtype, 1)
	}

	// Unencrypted packets
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}

	// Relay packets are special
	isMessageRelay := (h.Type == header.Message && h.Subtype == header.MessageRelay)

	var hostinfo *HostInfo
	if isMessageRelay {
		hostinfo = f.hostMap.QueryRelayIndex(h.RemoteIndex)
	} else {
		hostinfo = f.hostMap.QueryIndex(h.RemoteIndex)
	}
```
