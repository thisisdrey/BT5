Based on my research, I don't find a valid analog vulnerability in this repository.

The FileBrowser CVE describes an application-level bug in a custom TUS resumable-upload endpoint that fails to compare actual PATCH body size against a client-declared `Upload-Length` header before writing to disk. Go's standard library (`net/http`, `io`, `mime/multipart`) does not contain an equivalent "declared-length vs. actual-write" disk sink that skips validation:

- `net/http`'s body-length determination in `determineBodyLength` correctly sets `RealBodyLength` from a validated `Content-Length`, and the body reader is wrapped with `io.LimitReader(r, t.RealBodyLength)` so reads beyond the declared length are impossible at the transport layer. [1](#0-0) [2](#0-1) 

- `MaxBytesReader`/`maxBytesReader` (used by server handlers to self-limit body reads) strictly enforces the caller-supplied byte limit on every `Read`, returning `*MaxBytesError` once exceeded — the opposite of the FileBrowser flaw, which is the pattern that would need to be missing for an analog to exist. [3](#0-2) 

- `mime/multipart`'s `readForm`, which writes uploaded file parts to disk via `os.CreateTemp` + `io.CopyBuffer`, bounds the copy using `io.CopyN(&b, p, maxFileMemoryBytes+1)` and explicit `maxMemoryBytes`/`maxFileMemoryBytes` decrementing checks before any disk write occurs, so it cannot silently write past the client's declared/negotiated limits.
<invoke name="codebase_search">
<parameter name="query">nothing</parameter>
</invoke>

### Citations

**File:** src/net/http/transfer.go (L553-557)
```go
		}
	case t.RealBodyLength == 0:
		t.Body = NoBody
	case t.RealBodyLength > 0:
		t.Body = &body{src: io.LimitReader(r, t.RealBodyLength), closing: t.Close}
```

**File:** src/net/http/transfer.go (L753-757)
```go
	if parsedContentLength >= 0 {
		t.Header.Set("Content-Length", contentLengthStr)
		t.RealBodyLength = parsedContentLength
		t.ContentLength = parsedContentLength
		return nil
```

**File:** src/net/http/request.go (L1230-1270)
```go
func (l *maxBytesReader) Read(p []byte) (n int, err error) {
	if l.err != nil {
		return 0, l.err
	}
	if len(p) == 0 {
		return 0, nil
	}
	// If they asked for a 32KB byte read but only 5 bytes are
	// remaining, no need to read 32KB. 6 bytes will answer the
	// question of the whether we hit the limit or go past it.
	// 0 < len(p) < 2^63
	if int64(len(p))-1 > l.n {
		p = p[:l.n+1]
	}
	n, err = l.r.Read(p)

	if int64(n) <= l.n {
		l.n -= int64(n)
		l.err = err
		return n, err
	}

	n = int(l.n)
	l.n = 0

	// The server code and client code both use
	// maxBytesReader. This "requestTooLarge" check is
	// only used by the server code. To prevent binaries
	// which only using the HTTP Client code (such as
	// cmd/go) from also linking in the HTTP server, don't
	// use a static type assertion to the server
	// "*response" type. Check this interface instead:
	type requestTooLarger interface {
		requestTooLarge()
	}
	if res, ok := l.w.(requestTooLarger); ok {
		res.requestTooLarge()
	}
	l.err = &MaxBytesError{l.i}
	return n, l.err
}
```
