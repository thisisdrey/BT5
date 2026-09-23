Based on the investigation, this CVE analog does not map to a real vulnerability in this Go repository.

## Analysis

The Mosquitto CVE-2023-5632 root cause is that the MQTT broker registers `EPOLLOUT` unconditionally on a **level-triggered** epoll instance. Because level-triggered epoll re-reports "ready" on every `epoll_wait` call as long as the send buffer stays writable (which it always is on an idle new connection), the event loop spins at 100% CPU until data is actually written.

Go's runtime network poller does register `EPOLLIN | EPOLLOUT | EPOLLRDHUP` unconditionally for every socket in `netpollopen`, matching the "unconditional EPOLLOUT" surface pattern [1](#0-0) . However, it critically also sets `EPOLLET`, making the registration **edge-triggered** rather than level-triggered [2](#0-1) . With edge-triggered semantics, `epoll_wait` only reports the fd again when the readiness *state transitions* (e.g., buffer fills then drains), not on every poll call while it remains writable. This structurally prevents the busy-loop primitive that the Mosquitto bug relies on: `netpollready` is invoked only once per edge transition, and if no goroutine is parked waiting on that mode, it simply returns a delta without re-arming anything that would cause immediate re-firing [3](#0-2) .

For level-triggered backends (e.g., Solaris event ports), Go's implementation goes further and explicitly documents/handles the exact busy-loop risk this CVE class describes, re-associating only the subset of events not just delivered specifically "to avoid a busy loop" [4](#0-3) , and implements this via `netpollupdate` clearing consumed event bits before re-arming [5](#0-4) .

Since Go's netpoller design already avoids the level-triggered unconditional-EPOLLOUT pattern that caused CVE-2023-5632, and explicitly guards against the analogous busy-loop risk on level-triggered platforms, there is no reachable, unfixed analog of this bug in the scanned files.

### No vulnerability found for this question.

### Citations

**File:** src/runtime/netpoll_epoll.go (L64-69)
```go
func netpollopen(fd uintptr, pd *pollDesc) uintptr {
	var ev linux.EpollEvent
	ev.Events = linux.EPOLLIN | linux.EPOLLOUT | linux.EPOLLRDHUP | linux.EPOLLET
	netpollPackData(&ev, pd, pd.fdseq.Load())
	return linux.EpollCtl(epfd, linux.EPOLL_CTL_ADD, int32(fd), &ev)
}
```

**File:** src/runtime/netpoll.go (L493-510)
```go
//go:nowritebarrier
func netpollready(toRun *gList, pd *pollDesc, mode int32) int32 {
	delta := int32(0)
	var rg, wg *g
	if mode == 'r' || mode == 'r'+'w' {
		rg = netpollunblock(pd, 'r', true, &delta)
	}
	if mode == 'w' || mode == 'r'+'w' {
		wg = netpollunblock(pd, 'w', true, &delta)
	}
	if rg != nil {
		toRun.push(rg)
	}
	if wg != nil {
		toRun.push(wg)
	}
	return delta
}
```

**File:** src/runtime/netpoll_solaris.go (L54-59)
```go
// The logic dealing with re-associations is encapsulated in
// runtime·netpollupdate. This function takes care to associate the
// descriptor only with the subset of events that were previously
// part of the association, except the one that just happened. We
// can't re-associate with that right away, because event ports
// are level triggered so it would cause a busy loop. Instead, that
```

**File:** src/runtime/netpoll_solaris.go (L304-323)
```go
		var mode, clear int32
		if (ev.portev_events & (_POLLIN | _POLLHUP | _POLLERR)) != 0 {
			mode += 'r'
			clear |= _POLLIN
		}
		if (ev.portev_events & (_POLLOUT | _POLLHUP | _POLLERR)) != 0 {
			mode += 'w'
			clear |= _POLLOUT
		}
		// To effect edge-triggered events, we need to be sure to
		// update our association with whatever events were not
		// set with the event. For example if we are registered
		// for POLLIN|POLLOUT, and we get POLLIN, besides waking
		// the goroutine interested in POLLIN we have to not forget
		// about the one interested in POLLOUT.
		if clear != 0 {
			lock(&pd.lock)
			netpollupdate(pd, 0, uint32(clear))
			unlock(&pd.lock)
		}
```
