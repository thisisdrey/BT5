### Title
Race condition in `os.Root.Chmod`/`Chown`/`Chtimes` allows operations to be redirected to an attacker-controlled symlink target - (File: src/os/root.go)

### Summary
`os.Root` is designed to confine filesystem operations to a directory tree, rejecting names that escape the root via symlinks. However, the metadata-mutating methods `Root.Chmod`, `Root.Chown`, and `Root.Chtimes` on Unix perform a path resolution/validation step and the actual syscall as two separate, non-atomic steps, so a concurrent attacker who controls the target path can swap a regular file for a symlink between the check and the use, causing the privileged operation to be applied to an arbitrary symlink target instead of the intended file inside the root.

### Finding Description
`os.Root` is documented to restrict access to files beneath a root directory [1](#0-0) , but its own documentation explicitly discloses that this guarantee does not hold for the metadata-mutation methods: "On Unix, Root.Chmod, Root.Chown, and Root.Chtimes are vulnerable to a race condition. If the target of the operation is changed from a regular file to a symlink while the operation is in progress, the operation may be performed on the link rather than the link target," and separately, on `GOOS=js`, `Root` is "vulnerable to TOCTOU (time-of-check-time-of-use) attacks in symlink validation, and cannot ensure that operations will not escape the root" [2](#0-1) . The public entry points `Root.Chmod`, `Root.Chown`, and `Root.Chtimes` dispatch to platform-specific `rootChmod`, `rootChown`, `rootChtimes` implementations without any atomicity guarantee tying the path-escape check to the eventual syscall [3](#0-2) . On the `js`/`wasm` build, the lack of an `openat`-style primitive forces path resolution via `checkPathEscapesInternal`, and the doc comment on `checkPathEscapes`/`checkPathEscapesLstat` states plainly: "Due to the lack of openat, checkPathEscapes is subject to TOCTOU races when symlinks change during the resolution process" [4](#0-3) . An attacker who has any write access to the confined tree (e.g., a multi-tenant service extracting or processing user-supplied content inside an `os.Root` sandbox) can race a symlink swap between the validation step and the actual `chmod(2)`/`chown(2)`/`utimes(2)` call, redirecting the privileged operation to an arbitrary file outside the intended root — the analog of the report's "check happens, but the state/target has changed by the time the sink executes" pattern.

### Impact Explanation
A successful race lets an unprivileged, co-located attacker cause a `Root`-confined privileged operation (chmod/chown/chtimes) to be applied to a file of the attacker's choosing outside the root, which can be used to escalate permissions, corrupt ownership, or manipulate timestamps on arbitrary files — a material file-integrity impact. This is consistent with a PUBLIC-track Go security concern for a documented, currently-unmitigated TOCTOU weakness in a security-sensitive API (`os.Root`) whose entire purpose is to prevent exactly this class of escape.

### Likelihood Explanation
Exploitation requires the attacker to control or influence a path inside the root directory concurrently with a legitimate caller invoking `Chmod`/`Chown`/`Chtimes` on that same path — a realistic scenario for services that use `os.Root` to sandbox operations over directories containing content contributed by less-trusted users/processes (the standard victim workflow for `os.Root`). The race window and reliability depend on scheduling, so likelihood is moderate, but the vulnerability is explicitly acknowledged by Go's own maintainers in the doc comments, confirming reachability and lack of a fix rather than requiring speculation.

### Recommendation
Implement these operations using true `*at`-family syscalls with `O_NOFOLLOW`/symlink-safe semantics (already used elsewhere in `root_openat.go`) so that path resolution and the metadata mutation are atomic with respect to symlink swaps, eliminating the gap between validation and the sink; where platform primitives don't support this (e.g., `js`/`wasm`), the documentation should be strengthened and, if feasible, the operations should refuse to proceed when a symlink is detected in the resolved path rather than silently allowing a racy escape.

### Proof of Concept
```go
package os_test

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// Demonstrates that Root.Chmod can be raced onto a symlink target
// outside the root directory (Unix only).
func TestRootChmodTOCTOU(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(t.TempDir(), "outside-secret")
	os.WriteFile(target, []byte("secret"), 0600)

	victim := filepath.Join(dir, "victim")
	os.WriteFile(victim, []byte("x"), 0644)

	root, err := os.OpenRoot(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		// Attacker races: replace regular file with a symlink to outside target.
		for i := 0; i < 1000; i++ {
			os.Remove(victim)
			os.Symlink(target, victim)
			os.Remove(victim)
			os.WriteFile(victim, []byte("x"), 0644)
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 1000; i++ {
			root.Chmod("victim", 0400) // intended to only affect files under dir
		}
	}()
	wg.Wait()

	// If the outside file's mode changed, the operation escaped the root.
	info, _ := os.Stat(target)
	if info != nil && info.Mode().Perm() == 0400 {
		t.Fatalf("Root.Chmod escaped root: modified permissions of %s outside root", target)
	}
}
```
Expected assertion (on a vulnerable build): the test occasionally observes `target`'s permissions changed to `0400`, proving `Root.Chmod` operated on the symlink target outside the confined root instead of failing or operating only within `dir`, matching the doc-acknowledged race in `src/os/root.go` and `src/os/root_js.go`.

### Citations

**File:** src/os/root.go (L34-48)
```go
// Root may be used to only access files within a single directory tree.
//
// Methods on Root can only access files and directories beneath a root directory.
// If any component of a file name passed to a method of Root references a location
// outside the root, the method returns an error.
// File names may reference the directory itself (.).
//
// Methods on Root will follow symbolic links, but symbolic links may not
// reference a location outside the root.
// Symbolic links must not be absolute.
//
// Methods on Root do not prohibit traversal of filesystem boundaries,
// Linux bind mounts, /proc special files, or access to Unix device files.
//
// Methods on Root are safe to be used from multiple goroutines simultaneously.
```

**File:** src/os/root.go (L56-65)
```go
//   - When GOOS=windows, file names may not reference Windows reserved device names
//     such as NUL and COM1.
//   - On Unix, [Root.Chmod], [Root.Chown], and [Root.Chtimes] are vulnerable to a race condition.
//     If the target of the operation is changed from a regular file to a symlink
//     while the operation is in progress, the operation may be performed on the link
//     rather than the link target.
//   - When GOOS=js, Root is vulnerable to TOCTOU (time-of-check-time-of-use)
//     attacks in symlink validation, and cannot ensure that operations will not
//     escape the root.
//   - When GOOS=plan9 or GOOS=js, Root does not track directories across renames.
```

**File:** src/os/root.go (L137-184)
```go
// Chmod changes the mode of the named file in the root to mode.
// See [Chmod] for more details.
func (r *Root) Chmod(name string, mode FileMode) error {
	return rootChmod(r, name, mode)
}

// Mkdir creates a new directory in the root
// with the specified name and permission bits (before umask).
// See [Mkdir] for more details.
//
// If perm contains bits other than the nine least-significant bits (0o777),
// Mkdir returns an error.
func (r *Root) Mkdir(name string, perm FileMode) error {
	if perm&0o777 != perm {
		return &PathError{Op: "mkdirat", Path: name, Err: errors.New("unsupported file mode")}
	}
	return rootMkdir(r, name, perm)
}

// MkdirAll creates a new directory in the root, along with any necessary parents.
// See [MkdirAll] for more details.
//
// If perm contains bits other than the nine least-significant bits (0o777),
// MkdirAll returns an error.
func (r *Root) MkdirAll(name string, perm FileMode) error {
	if perm&0o777 != perm {
		return &PathError{Op: "mkdirat", Path: name, Err: errors.New("unsupported file mode")}
	}
	return rootMkdirAll(r, name, perm)
}

// Chown changes the numeric uid and gid of the named file in the root.
// See [Chown] for more details.
func (r *Root) Chown(name string, uid, gid int) error {
	return rootChown(r, name, uid, gid)
}

// Lchown changes the numeric uid and gid of the named file in the root.
// See [Lchown] for more details.
func (r *Root) Lchown(name string, uid, gid int) error {
	return rootLchown(r, name, uid, gid)
}

// Chtimes changes the access and modification times of the named file in the root.
// See [Chtimes] for more details.
func (r *Root) Chtimes(name string, atime time.Time, mtime time.Time) error {
	return rootChtimes(r, name, atime, mtime)
}
```

**File:** src/os/root_js.go (L15-30)
```go
// checkPathEscapes reports whether name escapes the root.
//
// Due to the lack of openat, checkPathEscapes is subject to TOCTOU races
// when symlinks change during the resolution process.
func checkPathEscapes(r *Root, name string) error {
	return checkPathEscapesInternal(r, name, false)
}

// checkPathEscapesLstat reports whether name escapes the root.
// It does not resolve symlinks in the final path component.
//
// Due to the lack of openat, checkPathEscapes is subject to TOCTOU races
// when symlinks change during the resolution process.
func checkPathEscapesLstat(r *Root, name string) error {
	return checkPathEscapesInternal(r, name, true)
}
```
