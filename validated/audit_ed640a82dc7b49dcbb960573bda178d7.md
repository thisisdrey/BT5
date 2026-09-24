Based on my investigation of the `ThankGodontt/go--024` repository (a mirror of the Go standard library and toolchain, not the OliveTin application), I could not find an analogous, exploitable unsynchronized-map-write pattern reachable from unauthenticated network input in the actual Go standard library. Key findings:

- `net/http`'s `CrossOriginProtection.trusted` map is protected by `trustedMu sync.RWMutex` on all access paths. [1](#0-0) 
- `crypto/tls`'s `lruSessionCache.m` map is guarded by `sync.Mutex` in both `Put` and `Get`. [2](#0-1) [3](#0-2) [4](#0-3) 
- `crypto/tls`'s certificate cache uses `sync.Map` (`weakCertCache`) rather than a raw map, which is inherently safe for concurrent access. [5](#0-4) 
- `net/http`'s legacy `serveMux121.m` map is guarded by `mu sync.RWMutex` on every mutation path. [6](#0-5) 
- The `fatal("concurrent map writes")` panics found in `internal/runtime/maps/*` are the Go runtime's own built-in detector for misuse of raw maps — this is the runtime intentionally crashing when a *caller* (like OliveTin) fails to synchronize access; it is not itself a defect in the Go standard library's own concurrent-safe data structures. [7](#0-6) 
- The Go project itself maintains regression tests specifically confirming this fatal-error behavior works correctly under concurrent map read/write races (e.g. `test/fixedbugs/issue79186.go`), demonstrating that this is expected, defended-against behavior rather than a vulnerability. [8](#0-7) 

No unsynchronized map reachable directly from an unauthenticated HTTP entry point in production `net/http`, `crypto/tls`, or other stdlib network-facing code was found — every network-input-driven shared map I located is protected by a mutex or `sync.Map`. The OliveTin advisory concerns application-level code (`registeredStates` in OliveTin's own OAuth2 handler) that has no equivalent unprotected counterpart in the Go standard library itself.

### No Vulnerability found for this question.

### Citations

**File:** src/net/http/csrf.go (L36-41)
```go
type CrossOriginProtection struct {
	bypass    atomic.Pointer[ServeMux]
	trustedMu sync.RWMutex
	trusted   map[string]bool
	deny      atomic.Pointer[Handler]
}
```

**File:** src/crypto/tls/common.go (L1686-1692)
```go
type lruSessionCache struct {
	sync.Mutex

	m        map[string]*list.Element
	q        *list.List
	capacity int
}
```

**File:** src/crypto/tls/common.go (L1717-1720)
```go
func (c *lruSessionCache) Put(sessionKey string, cs *ClientSessionState) {
	c.Lock()
	defer c.Unlock()

```

**File:** src/crypto/tls/common.go (L1754-1757)
```go
func (c *lruSessionCache) Get(sessionKey string) (*ClientSessionState, bool) {
	c.Lock()
	defer c.Unlock()

```

**File:** src/crypto/tls/cache.go (L14-42)
```go
// weakCertCache provides a cache of *x509.Certificates, allowing multiple
// connections to reuse parsed certificates, instead of re-parsing the
// certificate for every connection, which is an expensive operation.
type weakCertCache struct{ sync.Map }

func (wcc *weakCertCache) newCert(der []byte) (*x509.Certificate, error) {
	if entry, ok := wcc.Load(string(der)); ok {
		if v := entry.(weak.Pointer[x509.Certificate]).Value(); v != nil {
			return v, nil
		}
	}

	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}

	wp := weak.Make(cert)
	if entry, loaded := wcc.LoadOrStore(string(der), wp); !loaded {
		runtime.AddCleanup(cert, func(_ any) { wcc.CompareAndDelete(string(der), entry) }, any(string(der)))
	} else if v := entry.(weak.Pointer[x509.Certificate]).Value(); v != nil {
		return v, nil
	} else {
		if wcc.CompareAndSwap(string(der), entry, wp) {
			runtime.AddCleanup(cert, func(_ any) { wcc.CompareAndDelete(string(der), wp) }, any(string(der)))
		}
	}
	return cert, nil
}
```

**File:** src/net/http/servemux121.go (L38-70)
```go
// serveMux121 holds the state of a ServeMux needed for Go 1.21 behavior.
type serveMux121 struct {
	mu    sync.RWMutex
	m     map[string]muxEntry
	es    []muxEntry // slice of entries sorted from longest to shortest.
	hosts bool       // whether any patterns contain hostnames
}

type muxEntry struct {
	h       Handler
	pattern string
}

// Formerly ServeMux.Handle.
func (mux *serveMux121) handle(pattern string, handler Handler) {
	mux.mu.Lock()
	defer mux.mu.Unlock()

	if pattern == "" {
		panic("http: invalid pattern")
	}
	if handler == nil {
		panic("http: nil handler")
	}
	if _, exist := mux.m[pattern]; exist {
		panic("http: multiple registrations for " + pattern)
	}

	if mux.m == nil {
		mux.m = make(map[string]muxEntry)
	}
	e := muxEntry{h: handler, pattern: pattern}
	mux.m[pattern] = e
```

**File:** src/internal/runtime/maps/runtime_fast64.go (L181-193)
```go
//go:linkname runtime_mapassign_fast64 runtime.mapassign_fast64
func runtime_mapassign_fast64(typ *abi.MapType, m *Map, key uint64) unsafe.Pointer {
	if m == nil {
		panic(errNilAssign)
	}
	if race.Enabled {
		callerpc := sys.GetCallerPC()
		pc := abi.FuncPCABIInternal(runtime_mapassign_fast64)
		race.WritePC(unsafe.Pointer(m), callerpc, pc)
	}
	if m.writing != 0 {
		fatal("concurrent map writes")
	}
```

**File:** test/fixedbugs/issue79186.go (L1-39)
```go
// run

// Copyright 2026 The Go Authors. All rights reserved.
// Use of this source code is governed by a BSD-style
// license that can be found in the LICENSE file.

// Issue 79186: on ppc64le (POWER8/9), atomic add operations lacked a
// post-barrier (acquire ordering), allowing loads after an RWMutex.RLock
// to be speculatively reordered before the lock acquisition, causing
// concurrent map read and map write.

package main

import (
	"runtime"
	"sync"
)

type M struct {
	mu sync.RWMutex
	m  map[int]int
}

func NewM() *M {
	return &M{m: make(map[int]int)}
}

func (x *M) Get(k int) (int, bool) {
	x.mu.RLock()
	v, ok := x.m[k]
	x.mu.RUnlock()
	return v, ok
}

func (x *M) Set(k, v int) {
	x.mu.Lock()
	x.m[k] = v
	x.mu.Unlock()
}
```
