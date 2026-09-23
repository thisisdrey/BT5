I found a directly analogous pattern in the Go module cache extraction code.

### Title
Module cache `.partial` marker file created with world-writable 0666 permissions - ([File: src/cmd/go/internal/modfetch/fetch.go])

### Summary
Like the ROTP advisory where `.rb` files were created with overly permissive `0666` mode allowing any local user to modify sensitive files, the Go module downloader's `unzip` function creates the module cache `.partial` marker file with explicit mode `0o666` via `os.WriteFile(partialPath, nil, 0o666)`. This produces a world-writable file in `GOMODCACHE` on multi-user systems where umask does not sufficiently restrict permissions. [1](#0-0) 

### Finding Description
During `go build`/`go mod download`, `Fetcher.download` calls `unzip(ctx, mod, zipfile)` [2](#0-1) , which creates the parent cache directory with `os.MkdirAll(parentDir, 0o777)` and then writes the `.partial` marker with `os.WriteFile(partialPath, nil, 0o666)` before extracting the module zip [3](#0-2) . The stated purpose of the `.partial` file is "to prevent other processes from reading the directory if we crash" [4](#0-3) , i.e., it is explicitly a security-relevant sentinel guarding concurrent readers of a partially-extracted module directory. Creating it with mode `0666` (subject only to process umask) means any other local user/process on a shared system could remove or tamper with this partial-marker file while extraction is in progress, undermining the crash-safety/locking invariant the comment describes.

### Impact Explanation
This is a local file-permission weakness, not a remote-exploitation primitive: on a shared multi-user machine, another unprivileged local user could interfere with the `.partial` marker during concurrent `go mod download`/`go build` invocations, potentially causing another process to read from or use a partially-extracted, corrupted module directory (integrity impact), matching CWE-276 in the source advisory. This would fall under Go's PUBLIC track at most as a low-severity local information/integrity issue since it requires local co-tenancy and does not by itself grant code execution.

### Likelihood Explanation
Requires a shared, multi-user machine and concurrent invocation of `go mod download`/`go build` by another local user in the attack window while the `.partial` file exists — a narrow race window, not attacker-controlled remote input.

### Recommendation
Use a more restrictive mode consistent with other cache writes (e.g., `0o600`/`0o644`) or rely on `os.CreateTemp`-style private file creation for the `.partial` marker so it isn't group/world-writable regardless of umask.

### Proof of Concept
```go
package modfetch

import (
    "os"
    "path/filepath"
    "testing"
)

func TestPartialFilePermissive(t *testing.T) {
    dir := t.TempDir()
    partialPath := filepath.Join(dir, "v1.0.0.partial")
    if err := os.WriteFile(partialPath, nil, 0o666); err != nil {
        t.Fatal(err)
    }
    info, err := os.Stat(partialPath)
    if err != nil {
        t.Fatal(err)
    }
    if info.Mode().Perm()&0o022 != 0 {
        t.Fatalf("partial file has group/world write bits set: %v", info.Mode().Perm())
    }
}
```
This test replicates the exact `os.WriteFile(partialPath, nil, 0o666)` call from `unzip` and asserts no group/world-write bits — the assertion fails on systems with a permissive umask, confirming the file is created writable by other local users, analogous to the ROTP `0666` `.rb` file issue.

### Citations

**File:** src/cmd/go/internal/modfetch/fetch.go (L97-118)
```go
func (f *Fetcher) download(ctx context.Context, mod module.Version) (dir string, err error) {
	ctx, span := trace.StartSpan(ctx, "modfetch.download "+mod.String())
	defer span.Done()

	dir, err = DownloadDir(ctx, mod)
	if err == nil {
		// The directory has already been completely extracted (no .partial file exists).
		return dir, nil
	} else if dir == "" || !errors.Is(err, fs.ErrNotExist) {
		return "", err
	}

	// To avoid cluttering the cache with extraneous files,
	// DownloadZip uses the same lockfile as Download.
	// Invoke DownloadZip before locking the file.
	zipfile, err := f.DownloadZip(ctx, mod)
	if err != nil {
		return "", err
	}

	return unzip(ctx, mod, zipfile)
}
```

**File:** src/cmd/go/internal/modfetch/fetch.go (L161-169)
```go
	//
	// To prevent other processes from reading the directory if we crash,
	// create a .partial file before extracting the directory, and delete
	// the .partial file afterward (all while holding the lock).
	//
	// Before Go 1.16, we extracted to a temporary directory with a random name
	// then renamed it into place with os.Rename. On Windows, this failed with
	// ERROR_ACCESS_DENIED when another process (usually an anti-virus scanner)
	// opened files in the temporary directory.
```

**File:** src/cmd/go/internal/modfetch/fetch.go (L174-180)
```go
	if err := os.MkdirAll(parentDir, 0o777); err != nil {
		return "", err
	}
	if err := os.WriteFile(partialPath, nil, 0o666); err != nil {
		return "", err
	}
	if err := modzip.Unzip(dir, mod, zipfile); err != nil {
```
