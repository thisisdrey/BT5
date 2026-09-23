### Title
`go mod tidy`/`go get`/`go work sync` follow symlinks when writing project-local `go.mod`/`go.sum` state - ([File: src/cmd/go/internal/lockedfile/lockedfile.go])

### Summary
The `go` command persists project-local module state (`go.mod`, `go.sum`, `go.work.sum`) through `lockedfile.Edit`/`lockedfile.Transform`, which open the destination with plain `os.OpenFile(name, os.O_RDWR|os.O_CREATE, ...)` and no `Lstat`/`O_NOFOLLOW` guard. If an attacker-authored repository or module checkout contains `go.mod` (or `go.sum`) as a symlink, a victim running an ordinary command such as `go mod tidy`, `go get`, or `go work sync` in that checkout will write updated module-graph contents through the symlink into the target file, exactly mirroring the PDM `pdm.toml`/`.pdm-python` symlink-clobber pattern (CWE-61).

### Finding Description
Attacker input: a git repository (or module source tree) where the file `go.mod` (or `go.sum`) is checked in as a symbolic link to a path outside the repository, e.g. pointing at a file in the victim's home directory or another sensitive location. Victim workflow: clone/checkout the repository and run `go get`, `go mod tidy`, or `go work sync` inside it — a completely ordinary developer action.

- Entry point: `modload.commitRequirements` in [1](#0-0)  resolves `modFilePath := modFilePath(ld.MainModules.ModRoot(mainModule))` and calls `lockedfile.Transform(modFilePath, ...)` at [2](#0-1) .
- The related `go.sum` sink is `Fetcher.WriteGoSum`, which calls `lockedfile.Transform(f.goSumFile, ...)` at [3](#0-2) .
- Both funnel into `lockedfile.Edit` → `OpenFile`, which opens the name with `os.O_RDWR|os.O_CREATE` (no `O_EXCL`, no `O_NOFOLLOW`, no prior `Lstat` check) at [4](#0-3)  and [5](#0-4) .
- `Transform` then reads the current bytes and unconditionally overwrites/truncates them with the new module-graph contents at [6](#0-5) .

Because the destination path is opened by name with the OS's default symlink-following semantics, if `go.mod`/`go.sum` on disk is a symlink, the write lands on the symlink's target rather than being refused or redirected to a regular file inside the project.

### Impact Explanation
This is a project-local file write primitive: an attacker who can get a victim to run a standard module command (`go get`, `go mod tidy`, `go work sync`) inside an attacker-authored checkout can cause the `go` tool to overwrite an arbitrary file reachable by the symlink with attacker-influenced module-requirement/checksum text, as the invoking user. This is a file-clobber/integrity issue analogous to the PDM advisory, not remote code execution by itself, and would likely be assessed on Go's PUBLIC track as a hardening improvement to the module tooling rather than an urgent RCE, similar in class/severity to the referenced Medium-severity PDM CVE.

### Likelihood Explanation
The victim workflow (clone a repository, run `go get`/`go mod tidy`) is extremely common and does not require special privileges from the attacker beyond publishing an untrusted repository/module for the victim to check out and build within — matching the "AV:L, UI:A" style local-write scenario in the source report. No host, elevated privilege, or malicious-server capability is required.

### Recommendation
- Before writing project-local `go.mod`/`go.sum`/`go.work.sum`, `Lstat` the destination and refuse to proceed (or fail loudly) if it is a symlink, rather than transparently following it.
- Where the platform supports it, open with `O_NOFOLLOW` (or the Windows reparse-point-avoidance flag already used in `internal/syscall/windows/at_windows.go`'s `Openat`, see [7](#0-6) ) so the kernel itself rejects symlinked module-state files.
- Apply the same guard uniformly in `lockedfile.Edit`/`OpenFile` since it is the shared primitive used by all project-local state writers (`go.mod`, `go.sum`, `go.work`, `go.work.sum`).

### Proof of Concept
```go
package lockedfile_test

import (
	"os"
	"path/filepath"
	"testing"

	"cmd/go/internal/lockedfile"
)

// TestTransformFollowsSymlink demonstrates that lockedfile.Transform (used to
// write go.mod/go.sum in a project directory) will clobber a file outside the
// project if the project-local path is a symlink.
func TestTransformFollowsSymlink(t *testing.T) {
	dir := t.TempDir()

	// Simulate a file outside the "project" that an attacker wants clobbered.
	target := filepath.Join(dir, "outside-secret.txt")
	if err := os.WriteFile(target, []byte("original-untouched-content\n"), 0o666); err != nil {
		t.Fatal(err)
	}

	// Simulate an attacker-controlled checkout where go.mod is a symlink
	// pointing at the victim's file outside the repo.
	projectGoMod := filepath.Join(dir, "go.mod")
	if err := os.Symlink(target, projectGoMod); err != nil {
		t.Fatal(err)
	}

	// This mirrors what modload.commitRequirements does when it "writes go.mod".
	err := lockedfile.Transform(projectGoMod, func(old []byte) ([]byte, error) {
		return []byte("module clobbered\n\ngo 1.22\n"), nil
	})
	if err != nil {
		t.Fatalf("Transform failed: %v", err)
	}

	got, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) == "original-untouched-content\n" {
		t.Fatal("expected symlink target to be protected, but it was not modified (test setup issue)")
	}
	// Demonstrates the vulnerability: the file OUTSIDE the project directory
	// was overwritten via the symlinked go.mod path.
	t.Logf("outside file clobbered, new contents: %q", got)
}
```
Expected (vulnerable) result: `target` (`outside-secret.txt`, outside the simulated project) ends up containing `"module clobbered\n\ngo 1.22\n"` instead of its original content, proving that `lockedfile.Transform`/`Edit` — and therefore `go mod tidy`/`go get`/`go work sync` — write through a project-local symlink to an external target.

### Citations

**File:** src/cmd/go/internal/modload/init.go (L2060-2066)
```go
func commitRequirements(ld *Loader, ctx context.Context, opts WriteOpts) (err error) {
	if ld.inWorkspaceMode() {
		// go.mod files aren't updated in workspace mode, but we still want to
		// update the go.work.sum file.
		return ld.Fetcher().WriteGoSum(ctx, keepSums(ld, ctx, ld.pkgLoader, ld.requirements, addBuildListZipSums), mustHaveCompleteRequirements(ld))
	}
	_, updatedGoMod, modFile, err := UpdateGoModFromReqs(ld, ctx, opts)
```

**File:** src/cmd/go/internal/modload/init.go (L2118-2140)
```go
	if unlock, err := modfetch.SideLock(ctx); err == nil {
		defer unlock()
	}

	err = lockedfile.Transform(modFilePath, func(old []byte) ([]byte, error) {
		if bytes.Equal(old, updatedGoMod) {
			// The go.mod file is already equal to new, possibly as the result of some
			// other process.
			return nil, errNoChange
		}

		if index != nil && !bytes.Equal(old, index.data) {
			// The contents of the go.mod file have changed. In theory we could add all
			// of the new modules to the build list, recompute, and check whether any
			// module in *our* build list got bumped to a different version, but that's
			// a lot of work for marginal benefit. Instead, fail the command: if users
			// want to run concurrent commands, they need to start with a complete,
			// consistent module definition.
			return nil, fmt.Errorf("existing contents have changed since last read")
		}

		return updatedGoMod, nil
	})
```

**File:** src/cmd/go/internal/modfetch/fetch.go (L982-988)
```go
	err := lockedfile.Transform(f.goSumFile, func(data []byte) ([]byte, error) {
		tidyGoSum := tidyGoSum(f, data, keep)
		return tidyGoSum, nil
	})
	if err != nil {
		return fmt.Errorf("updating go.sum: %w", err)
	}
```

**File:** src/cmd/go/internal/lockedfile/lockedfile.go (L40-48)
```go
func OpenFile(name string, flag int, perm fs.FileMode) (*File, error) {
	var (
		f   = new(File)
		err error
	)
	f.osFile.File, err = openFile(name, flag, perm)
	if err != nil {
		return nil, err
	}
```

**File:** src/cmd/go/internal/lockedfile/lockedfile.go (L72-79)
```go
// Edit creates the named file with mode 0666 (before umask),
// but does not truncate existing contents.
//
// If Edit succeeds, methods on the returned File can be used for I/O.
// The associated file descriptor has mode O_RDWR and the file is write-locked.
func Edit(name string) (*File, error) {
	return OpenFile(name, os.O_RDWR|os.O_CREATE, 0666)
}
```

**File:** src/cmd/go/internal/lockedfile/lockedfile.go (L138-193)
```go
func Transform(name string, t func([]byte) ([]byte, error)) (err error) {
	f, err := Edit(name)
	if err != nil {
		return err
	}
	defer f.Close()

	old, err := io.ReadAll(f)
	if err != nil {
		return err
	}

	new, err := t(old)
	if err != nil {
		return err
	}

	if len(new) > len(old) {
		// The overall file size is increasing, so write the tail first: if we're
		// about to run out of space on the disk, we would rather detect that
		// failure before we have overwritten the original contents.
		if _, err := f.WriteAt(new[len(old):], int64(len(old))); err != nil {
			// Make a best effort to remove the incomplete tail.
			f.Truncate(int64(len(old)))
			return err
		}
	}

	// We're about to overwrite the old contents. In case of failure, make a best
	// effort to roll back before we close the file.
	defer func() {
		if err != nil {
			if _, err := f.WriteAt(old, 0); err == nil {
				f.Truncate(int64(len(old)))
			}
		}
	}()

	if len(new) >= len(old) {
		if _, err := f.WriteAt(new[:len(old)], 0); err != nil {
			return err
		}
	} else {
		if _, err := f.WriteAt(new, 0); err != nil {
			return err
		}
		// The overall file size is decreasing, so shrink the file to its final size
		// after writing. We do this after writing (instead of before) so that if
		// the write fails, enough filesystem space will likely still be reserved
		// to contain the previous contents.
		if err := f.Truncate(int64(len(new))); err != nil {
			return err
		}
	}

	return nil
```

**File:** src/internal/syscall/windows/at_windows.go (L130-132)
```go
	case flag&(syscall.O_CREAT|syscall.O_EXCL) == (syscall.O_CREAT | syscall.O_EXCL):
		disposition = FILE_CREATE
		options |= FILE_OPEN_REPARSE_POINT // don't follow symlinks
```
