### Title
Insecure temporary file handling via non-exclusive derived filename in cgo build tool - (File: src/cmd/cgo/util.go)

### Summary
`cmd/cgo`'s `run` function creates a temp file securely with `os.CreateTemp` (which uses `O_EXCL`), but immediately closes it and derives a second filename (`name+".c"`) that is written with `os.WriteFile`, which opens files with `O_CREATE|O_TRUNC` and **not** `O_EXCL`. This reproduces the same CWE-377 predictable/non-exclusive temp-file pattern described in the Robocode advisory: a securely-named temp file is created, but a related, predictably-derived path is written without exclusive-creation protection, opening a TOCTOU window in the shared, world-writable temp directory.

### Finding Description
In `run()` in `src/cmd/cgo/util.go`, when a compiler is invoked with `-xc -` (reading from stdin), cgo does: [1](#0-0) 
`os.CreateTemp("", "cgo-gcc-input-")` correctly uses `OpenFile(..., O_RDWR|O_CREATE|O_EXCL, 0600)` internally: [2](#0-1) 
so the base file `name` cannot be hijacked. However, cgo immediately closes that file (`f.Close()`) and instead writes the actual C source to a *derived* path, `name+".c"`, via `os.WriteFile`. `os.WriteFile` does not pass `O_EXCL`; it opens (or creates) the target with truncate semantics and follows symlinks. Because the random suffix chosen by `CreateTemp` becomes visible in the shared temp directory the moment `f` is created (before `Close()`/`WriteFile` run), a local attacker monitoring the world-writable temp directory (e.g., via `inotify`) can win the race: after seeing `cgo-gcc-input-<rand>` appear, it creates a symlink at `cgo-gcc-input-<rand>.c` pointing at an attacker-chosen target before cgo's `WriteFile` call executes. `WriteFile` then follows the symlink and overwrites the attacker's chosen target with the cgo-preprocessed C source content — a material, attacker-directed file write outside the intended temp file, matching the vulnerability class in the report (createTempFile-derived path lacking exclusive creation, enabling race-condition exploitation).

### Impact Explanation
An attacker with only ordinary local access to the shared temp directory (default `/tmp` on Unix, world-writable) can cause the `go build`/`cgo` process — potentially running with a developer's or CI's privileges — to overwrite an arbitrary file the victim process has write access to, via the dangling symlink race. This is a material file write via TOCTOU, matching the CWE-377 pattern. It does not involve running attacker-supplied Go source at build time (that remains out of scope per Go's build-safety guarantee), so this would land at most on the PRIVATE/low-severity track rather than PUBLIC/URGENT, since exploitation requires local, precisely-timed filesystem-race capability rather than remote/network input.

### Likelihood Explanation
Requires a co-resident, unprivileged local user with write access to the same OS temp directory as the victim's `cgo`/`go build` invocation, along with the ability to race the very short window between `os.CreateTemp` (which reveals the random filename to any file-system watcher) and the subsequent `os.WriteFile` on the derived `.c` path. This is a classic but narrow local TOCTOU condition; it is not exploitable purely via untrusted network input or a malicious module/archive alone.

### Recommendation
Avoid deriving a second, non-exclusively-created filename from a `CreateTemp` result. Instead, either write the `.c` content directly to the file descriptor already returned by `CreateTemp` (renaming/using a `*.c` pattern directly in `CreateTemp`'s pattern argument, e.g. `os.CreateTemp("", "cgo-gcc-input-*.c")`), or open the derived path with `O_EXCL` (and reject symlinks, e.g. via `os.OpenFile(name+".c", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)`) before writing.

### Proof of Concept
```go
package main

import (
	"os"
	"testing"
)

// Demonstrates that the second, derived filename lacks O_EXCL protection,
// unlike the base file produced by os.CreateTemp.
func TestDerivedTempFileNotExclusive(t *testing.T) {
	f, err := os.CreateTemp("", "cgo-gcc-input-")
	if err != nil {
		t.Fatal(err)
	}
	name := f.Name()
	f.Close()
	defer os.Remove(name)

	// Attacker (or race) pre-creates the derived path as a symlink to a
	// victim file before cgo's WriteFile call.
	victim, _ := os.CreateTemp("", "victim-")
	victim.Close()
	defer os.Remove(victim.Name())

	if err := os.Symlink(victim.Name(), name+".c"); err != nil {
		t.Fatal(err)
	}
	defer os.Remove(name + ".c")

	// This mirrors cmd/cgo/util.go's os.WriteFile(name+".c", stdin, 0666)
	if err := os.WriteFile(name+".c", []byte("attacker-controlled overwrite"), 0666); err != nil {
		t.Fatal(err)
	}

	data, _ := os.ReadFile(victim.Name())
	if string(data) != "attacker-controlled overwrite" {
		t.Fatalf("expected victim file to be overwritten via symlink, got: %q", data)
	}
	// Assertion succeeding demonstrates WriteFile followed the symlink and
	// overwrote the victim's file without O_EXCL protection.
}
```

### Citations

**File:** src/cmd/cgo/util.go (L25-36)
```go
		f, err := os.CreateTemp("", "cgo-gcc-input-")
		if err != nil {
			fatalf("%s", err)
		}
		name := f.Name()
		f.Close()
		if err := os.WriteFile(name+".c", stdin, 0666); err != nil {
			os.Remove(name)
			fatalf("%s", err)
		}
		defer os.Remove(name)
		defer os.Remove(name + ".c")
```

**File:** src/os/tempfile.go (L46-57)
```go
	try := 0
	for {
		name := prefix + nextRandom() + suffix
		f, err := OpenFile(name, O_RDWR|O_CREATE|O_EXCL, 0600)
		if IsExist(err) {
			if try++; try < 10000 {
				continue
			}
			return nil, &PathError{Op: "createtemp", Path: prefix + "*" + suffix, Err: ErrExist}
		}
		return f, err
	}
```
