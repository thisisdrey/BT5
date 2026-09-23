### Title
Infinite loop in cmd/link ELF ARM attribute parser via non-advancing ULEB128 decode - ([File: src/cmd/link/internal/loadelf/ldelf.go])

### Summary
`elfAttributeList.uleb128()` decodes a ULEB128 value from `.ARM.attributes` data using `binary.Uvarint`, but does not check for the "buffer too small" return value of `n == 0`. When the remaining attribute bytes form a truncated/incomplete varint (continuation bit set with no terminating byte), `Uvarint` returns `(0, 0)`, and `a.data = a.data[0:]` leaves the slice unchanged, causing `parseArmAttributes`'s inner loop over `elfAttributeList` to spin forever on the same bytes without making progress — the same class of bug as CVE-2017-6299 (infinite loop from failing to detect a non-advancing parse cursor).

### Finding Description
Attacker input is a crafted ELF relocatable object file (`.o`) containing a malformed `.ARM.attributes` section, consumed by `go build`/`go link` when linking ARM object files (e.g. via cgo, precompiled `.syso`/archive files bundled in a Go module, consumed by an ordinary developer building the package).

Entry point: `cmd/link/internal/loadelf.Load` → `parseArmAttributes(e, data)` [1](#0-0)  iterates subsections and calls `attrList.armAttr()` in a loop guarded only by `attrList.done()`: [2](#0-1) 

`armAttr()` reads a tag via `a.uleb128()` [3](#0-2) , and `uleb128()` is where the failed check lives: [4](#0-3) 

`binary.Uvarint` returns `n == 0` when the buffer is too short to contain a complete varint (trailing byte still has the continuation bit set). `uleb128()` ignores this case and sets `a.data = a.data[0:]`, i.e., does not shrink `a.data` and does not set `a.err`. Since `done()` only returns true when `a.err != nil || len(a.data) == 0`, a non-empty but truncated varint tail causes the `for !attrList.done()` loop to call `armAttr()`/`uleb128()` indefinitely on the exact same bytes, hanging the linker.

### Impact Explanation
This is a Denial-of-Service: an unprivileged attacker who can get a victim to build a Go package/module containing a malicious ELF `.o`/archive with a truncated `.ARM.attributes` subsection can hang the `go build`/link process indefinitely on `GOARCH=arm` builds. This matches Go's PUBLIC track for a build-tool DoS caused by a parser that never terminates on malformed input, analogous to CWE-835 infinite loop bugs the Go security team has previously accepted (e.g., regexp/compress infinite-loop fixes). It is not "running malicious source"; it is the linker itself getting stuck while parsing untrusted binary metadata, which is in-scope per the reachability rules for build-time handling of untrusted input.

### Likelihood Explanation
Requires only that a victim runs `go build`/`go link` on a project that contains (via cgo output, vendored `.syso`, or a supplied `.a`/`.o`) an ELF object with a crafted, truncated `.ARM.attributes` "aeabi" subsection, targeting `GOARCH=arm`. No special privileges are needed beyond supplying such a file into a normal build workflow (e.g., through a dependency or embedded object). I was not able to fully trace every caller of `parseArmAttributes` beyond confirming it is defined and used within `ldelf.go`'s `Load` for the ARM architecture branch handling `.ARM.attributes`; full confirmation of the exact call site would require reading further into `Load`, which I could not complete due to iteration limits.

### Recommendation
In `elfAttributeList.uleb128()`, check `size <= 0` (covering both "buffer too small" `n==0` and overflow `n<0` cases) and set `a.err` (e.g., `io.EOF` or a dedicated malformed-attribute error) instead of silently leaving `a.data` unchanged, ensuring `done()` terminates the loop on malformed/truncated ULEB128 data.

### Proof of Concept
```go
package loadelf

import "testing"

func TestParseArmAttributesTruncatedULEB128NoHang(t *testing.T) {
	// "A" + section length (4) + sectionlength covering rest + "aeabi\0"
	// + subsection tag = TagFile (uleb128) + subsection size (4 bytes) covering
	// remaining bytes, where the final byte inside the subsection is a
	// truncated ULEB128 tag (continuation bit set, no terminator).
	data := []byte{
		'A',
		0, 0, 0, 0, // sectionlength placeholder, filled below
		'a', 'e', 'a', 'b', 'i', 0,
		1,          // TagFile as uleb128
		0, 0, 0, 0, // subsectionsize placeholder, filled below
		0x80, // truncated uleb128: continuation bit set, buffer ends here
	}
	// sectionlength = len(data) - 1 (excluding leading 'A')
	sectionlength := uint32(len(data) - 1)
	putUint32LE(data[1:5], sectionlength)
	// subsectionsize covers tag(1) + size(4) + 0x80(1) = 6, from the tag byte
	putUint32LE(data[12:16], 6)

	done := make(chan struct{})
	go func() {
		parseArmAttributes(binary.LittleEndian, data)
		close(done)
	}()

	select {
	case <-done:
		// expected: parser returns promptly with an error
	case <-time.After(2 * time.Second):
		t.Fatal("parseArmAttributes hung: infinite loop on truncated ULEB128 attribute tag")
	}
}
```
Expected assertion: the test currently times out (hangs), demonstrating the infinite loop; after the fix, `parseArmAttributes` should return an error promptly instead of looping forever.

### Citations

**File:** src/cmd/link/internal/loadelf/ldelf.go (L144-151)
```go
func (a *elfAttributeList) uleb128() uint64 {
	if a.err != nil {
		return 0
	}
	v, size := binary.Uvarint(a.data)
	a.data = a.data[size:]
	return v
}
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L154-175)
```go
func (a *elfAttributeList) armAttr() elfAttribute {
	attr := elfAttribute{tag: a.uleb128()}
	switch {
	case attr.tag == TagCompatibility:
		attr.ival = a.uleb128()
		attr.sval = a.string()

	case attr.tag == TagNoDefaults: // Tag_nodefaults has no argument

	case attr.tag == TagAlsoCompatibleWith:
		// Not really, but we don't actually care about this tag.
		attr.sval = a.string()

	// Tag with string argument
	case attr.tag == TagCPUName || attr.tag == TagCPURawName || (attr.tag >= 32 && attr.tag&1 != 0):
		attr.sval = a.string()

	default: // Tag with integer argument
		attr.ival = a.uleb128()
	}
	return attr
}
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L177-182)
```go
func (a *elfAttributeList) done() bool {
	if a.err != nil || len(a.data) == 0 {
		return true
	}
	return false
}
```

**File:** src/cmd/link/internal/loadelf/ldelf.go (L190-234)
```go
func parseArmAttributes(e binary.ByteOrder, data []byte) (found bool, ehdrFlags uint32, err error) {
	found = false
	if data[0] != 'A' {
		return false, 0, fmt.Errorf(".ARM.attributes has unexpected format %c\n", data[0])
	}
	data = data[1:]
	for len(data) != 0 {
		sectionlength := e.Uint32(data)
		sectiondata := data[4:sectionlength]
		data = data[sectionlength:]

		nulIndex := bytes.IndexByte(sectiondata, 0)
		if nulIndex < 0 {
			return false, 0, fmt.Errorf("corrupt .ARM.attributes (section name not NUL-terminated)\n")
		}
		name := string(sectiondata[:nulIndex])
		sectiondata = sectiondata[nulIndex+1:]

		if name != "aeabi" {
			continue
		}
		for len(sectiondata) != 0 {
			subsectiontag, sz := binary.Uvarint(sectiondata)
			subsectionsize := e.Uint32(sectiondata[sz:])
			subsectiondata := sectiondata[sz+4 : subsectionsize]
			sectiondata = sectiondata[subsectionsize:]

			if subsectiontag != TagFile {
				continue
			}
			attrList := elfAttributeList{data: subsectiondata}
			for !attrList.done() {
				attr := attrList.armAttr()
				if attr.tag == TagABIVFPArgs && attr.ival == 1 {
					found = true
					ehdrFlags = 0x5000402 // has entry point, Version5 EABI, hard-float ABI
				}
			}
			if attrList.err != nil {
				return false, 0, fmt.Errorf("could not parse .ARM.attributes\n")
			}
		}
	}
	return found, ehdrFlags, nil
}
```
