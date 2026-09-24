### Title
Symlink following in predictable `/tmp` lock/status files allows arbitrary file write - ([File: misc/go_android_exec/main.go])

### Summary
`misc/go_android_exec/main.go` and `misc/ios/go_ios_exec.go` create files at fixed, predictable paths inside the shared, world-writable `os.TempDir()` (e.g. `go_android_exec-adb-lock`, `go_android_exec-adb-sync-status`, `go_ios_exec-<deviceID>.lock`) using `os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0666)` **without `O_EXCL`**. Because the name is predictable and the open call does not exclude existing paths, a local unprivileged attacker who pre-creates a symlink at that path can redirect the open (and, in the `adbCopyGoroot` case, a subsequent `WriteString`) to an arbitrary file the victim user can write to, producing corruption/DoS analogous to the LACT symlink-following report.

### Finding Description
Attacker (any local unprivileged user on the same host) creates a symlink at a predictable path in the shared `/tmp` directory before the victim runs `GOOS=android go test ...` or `GOOS=ios go test ...`, which invoke these exec wrappers as subprocesses of the `go` command.

- Entry point: `runMain()` in `misc/go_android_exec/main.go`, which computes: [1](#0-0) 
`lockPath := filepath.Join(os.TempDir(), "go_android_exec-adb-lock")` then `os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0666)` — no `O_EXCL`, so if `lockPath` is a symlink, the kernel follows it and opens the target for read/write.

- A stronger sink is in `adbCopyGoroot()`: [2](#0-1) 
`statPath := filepath.Join(os.TempDir(), "go_android_exec-adb-sync-status")`, opened the same way, then read fully and later: [3](#0-2) 
`stat.WriteString(goVersion)` — this appends attacker-uncontrolled-but-victim-triggered data to whatever file the symlink points to, since the position is already at EOF after `io.ReadAll`.

- The equivalent pattern exists in `misc/ios/go_ios_exec.go`: [4](#0-3) 
`lockName := filepath.Join(os.TempDir(), "go_ios_exec-"+deviceID+".lock")`, opened with `os.O_CREATE|os.O_RDONLY, 0666` (no `O_EXCL`), then `syscall.Flock`'d.

By contrast, the standard-library temp-file helpers correctly defend against this class of bug by always using `O_CREATE|O_EXCL` with randomized names: `os.CreateTemp`/`os.MkdirTemp` in [5](#0-4)  and the internal module cache helper `tempFile` in [6](#0-5) . The `misc/go_android_exec` and `misc/ios/go_ios_exec.go` wrappers deviate from this pattern by using fixed names and omitting `O_EXCL`.

### Impact Explanation
A local attacker can pre-plant a symlink at the fixed path in the shared `/tmp` and cause the victim's `go test` invocation (when cross-compiling for `android`/`ios`) to open and, in the `adbCopyGoroot` case, append content into an arbitrary file the victim user owns/can write — a material file write / local denial-of-service (e.g. corrupting a dotfile, config, or another user-writable file), matching the "local denial-of-service via symlink following" class in the LACT report. This is a build/test-tooling issue confined to the `misc/` exec wrappers, not the Go runtime or `go build` itself, so it would be assessed on Go's PUBLIC track as a low/medium-severity local tooling hardening issue rather than an urgent remote-exploitation bug.

### Likelihood Explanation
Requires a shared multi-user host where the victim runs `GOOS=android go test` or `GOOS=ios go test` (common in Android/iOS CI cross-build workflows) while an unprivileged co-tenant attacker has pre-created the predictable symlink in `/tmp` beforehand — a realistic scenario on shared build machines/CI runners.

### Recommendation
Use `os.OpenFile` with `O_CREATE|O_EXCL` (or `os.CreateTemp`/`os.MkdirTemp` with randomized names) for the lock and status files in `misc/go_android_exec/main.go` and `misc/ios/go_ios_exec.go`, or place them in a per-user, non-world-writable directory (e.g. under `os.UserCacheDir()`), rejecting pre-existing symlinks.

### Proof of Concept
```go
package poc

import (
	"os"
	"path/filepath"
	"testing"
)

// Reproduces the vulnerable open pattern used in
// misc/go_android_exec/main.go's adbCopyGoroot():
//   statPath := filepath.Join(os.TempDir(), "go_android_exec-adb-sync-status")
//   stat, _ := os.OpenFile(statPath, os.O_CREATE|os.O_RDWR, 0666)
func TestSymlinkFollowedOnPredictableTempPath(t *testing.T) {
	tmp := t.TempDir()

	// Attacker-controlled victim target the attacker should NOT be able to touch.
	victim := filepath.Join(tmp, "victim-secret-file")
	if err := os.WriteFile(victim, []byte("original"), 0600); err != nil {
		t.Fatal(err)
	}

	// Attacker pre-creates a symlink at the predictable, fixed path.
	predictable := filepath.Join(tmp, "go_android_exec-adb-sync-status")
	if err := os.Symlink(victim, predictable); err != nil {
		t.Fatal(err)
	}

	// Vulnerable pattern: O_CREATE|O_RDWR without O_EXCL on a predictable name.
	f, err := os.OpenFile(predictable, os.O_CREATE|os.O_RDWR, 0666)
	if err != nil {
		t.Fatalf("open followed error unexpectedly: %v", err)
	}
	defer f.Close()

	// Simulate the later stat.WriteString(goVersion) write.
	if _, err := f.WriteString("attacker-triggered-write"); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	// Assertion: the "victim" file (not the predictable temp path) was mutated,
	// proving the symlink was followed and an unintended file was written.
	if string(data) != "originalattacker-triggered-write" {
		t.Fatalf("expected victim file to be modified via symlink, got %q", data)
	}
}
```
Expected: the test passes, demonstrating that the fixed-name, non-`O_EXCL` open in `misc/go_android_exec/main.go` would follow an attacker-planted symlink and write into an unintended file.

### Citations

**File:** misc/go_android_exec/main.go (L99-104)
```go
	lockPath := filepath.Join(os.TempDir(), "go_android_exec-adb-lock")
	lock, err := os.OpenFile(lockPath, os.O_CREATE|os.O_RDWR, 0666)
	if err != nil {
		return 0, err
	}
	defer lock.Close()
```

**File:** misc/go_android_exec/main.go (L380-396)
```go
	statPath := filepath.Join(os.TempDir(), "go_android_exec-adb-sync-status")
	stat, err := os.OpenFile(statPath, os.O_CREATE|os.O_RDWR, 0666)
	if err != nil {
		return err
	}
	defer stat.Close()
	// Serialize check and copying.
	if err := syscall.Flock(int(stat.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	s, err := io.ReadAll(stat)
	if err != nil {
		return err
	}
	if string(s) == goVersion {
		return nil
	}
```

**File:** misc/go_android_exec/main.go (L480-482)
```go
	if _, err := stat.WriteString(goVersion); err != nil {
		return err
	}
```

**File:** misc/ios/go_ios_exec.go (L81-88)
```go
	lockName := filepath.Join(os.TempDir(), "go_ios_exec-"+deviceID+".lock")
	lock, err = os.OpenFile(lockName, os.O_CREATE|os.O_RDONLY, 0666)
	if err != nil {
		return 1, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return 1, err
	}
```

**File:** src/os/tempfile.go (L46-58)
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
}
```

**File:** src/cmd/go/internal/modfetch/cache.go (L713-727)
```go
// tempFile creates a new temporary file with given permission bits.
func tempFile(ctx context.Context, dir, prefix string, perm fs.FileMode) (f *os.File, err error) {
	for i := 0; i < 10000; i++ {
		name := filepath.Join(dir, prefix+strconv.Itoa(rand.Intn(1000000000))+".tmp")
		f, err = os.OpenFile(name, os.O_RDWR|os.O_CREATE|os.O_EXCL, perm)
		if os.IsExist(err) {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			continue
		}
		break
	}
	return
}
```
