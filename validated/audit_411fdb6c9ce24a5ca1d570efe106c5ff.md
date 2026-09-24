No vulnerability found for this question.

The Group-Office CVE describes an application-level command-injection bug: extracting attacker-controlled filenames from a TNEF/`winmail.dat` attachment and then invoking the `zip` shell command with a wildcard argument, so a crafted filename is interpreted as a `zip` CLI option. Searching the go--021 repository (a mirror of the Go standard library / toolchain source) for the equivalent primitive — archive extraction whose output filenames are subsequently passed to an external command via a shell/wildcard invocation — turns up no analogous path.

The `archive/zip` package itself only performs in-memory reading/writing of zip entries and never shells out. [1](#0-0) . The module-cache unzip flow (`cmd/go/internal/modfetch/fetch.go`'s `unzip`) extracts trusted, checksum-verified module zips via the pure-Go `modzip.Unzip` function directly to disk, never invoking an external `zip`/shell command with extracted filenames. [2](#0-1) . All the `exec.Command` call sites found (`cmd/cgo/util.go`, `cmd/go/internal/generate/generate.go`, `cmd/go/internal/modfetch/codehost/codehost.go`, `os/exec` examples, various test helpers) either pass a fixed, hardcoded argv or explicitly quote/escape arguments for logging (see `bashQuoter` in `codehost.go`) rather than concatenating untrusted, archive-derived filenames into a shell wildcard expression. [3](#0-2) [4](#0-3) .

None of these represent the same root-cause primitive (extracted/untrusted filenames flowing into a shell command that interprets `*`/option-like names as flags), so no valid analog vulnerability exists in this repository for this report.

### Citations

**File:** src/archive/zip/example_test.go (L50-72)
```go
func ExampleReader() {
	// Open a zip archive for reading.
	r, err := zip.OpenReader("testdata/readme.zip")
	if err != nil {
		log.Fatal(err)
	}
	defer r.Close()

	// Iterate through the files in the archive,
	// printing some of their contents.
	for _, f := range r.File {
		fmt.Printf("Contents of %s:\n", f.Name)
		rc, err := f.Open()
		if err != nil {
			log.Fatal(err)
		}
		_, err = io.CopyN(os.Stdout, rc, 68)
		if err != nil {
			log.Fatal(err)
		}
		rc.Close()
		fmt.Println()
	}
```

**File:** src/cmd/go/internal/modfetch/fetch.go (L120-186)
```go
func unzip(ctx context.Context, mod module.Version, zipfile string) (dir string, err error) {
	unlock, err := lockVersion(ctx, mod)
	if err != nil {
		return "", err
	}
	defer unlock()

	ctx, span := trace.StartSpan(ctx, "unzip "+zipfile)
	defer span.Done()

	// Check whether the directory was populated while we were waiting on the lock.
	dir, dirErr := DownloadDir(ctx, mod)
	if dirErr == nil {
		return dir, nil
	}
	_, dirExists := dirErr.(*DownloadDirPartialError)

	// Clean up any remaining temporary directories created by old versions
	// (before 1.16), as well as partially extracted directories (indicated by
	// DownloadDirPartialError, usually because of a .partial file). This is only
	// safe to do because the lock file ensures that their writers are no longer
	// active.
	parentDir := filepath.Dir(dir)
	tmpPrefix := filepath.Base(dir) + ".tmp-"
	if old, err := filepath.Glob(filepath.Join(str.QuoteGlob(parentDir), str.QuoteGlob(tmpPrefix)+"*")); err == nil {
		for _, path := range old {
			RemoveAll(path) // best effort
		}
	}
	if dirExists {
		if err := RemoveAll(dir); err != nil {
			return "", err
		}
	}

	partialPath, err := CachePath(ctx, mod, "partial")
	if err != nil {
		return "", err
	}

	// Extract the module zip directory at its final location.
	//
	// To prevent other processes from reading the directory if we crash,
	// create a .partial file before extracting the directory, and delete
	// the .partial file afterward (all while holding the lock).
	//
	// Before Go 1.16, we extracted to a temporary directory with a random name
	// then renamed it into place with os.Rename. On Windows, this failed with
	// ERROR_ACCESS_DENIED when another process (usually an anti-virus scanner)
	// opened files in the temporary directory.
	//
	// Go 1.14.2 and higher respect .partial files. Older versions may use
	// partially extracted directories. 'go mod verify' can detect this,
	// and 'go clean -modcache' can fix it.
	if err := os.MkdirAll(parentDir, 0o777); err != nil {
		return "", err
	}
	if err := os.WriteFile(partialPath, nil, 0o666); err != nil {
		return "", err
	}
	if err := modzip.Unzip(dir, mod, zipfile); err != nil {
		fmt.Fprintf(os.Stderr, "-> %s\n", err)
		if rmErr := RemoveAll(dir); rmErr == nil {
			os.Remove(partialPath)
		}
		return "", err
	}
```

**File:** src/cmd/go/internal/modfetch/codehost/codehost.go (L324-366)
```go
// bashQuoter escapes characters that have special meaning in double-quoted strings in the bash shell.
// See https://www.gnu.org/software/bash/manual/html_node/Double-Quotes.html.
var bashQuoter = strings.NewReplacer(`"`, `\"`, `$`, `\$`, "`", "\\`", `\`, `\\`)

func run(ctx context.Context, args RunArgs) ([]byte, error) {
	if args.dir != "" {
		muIface, ok := dirLock.Load(args.dir)
		if !ok {
			muIface, _ = dirLock.LoadOrStore(args.dir, new(sync.Mutex))
		}
		mu := muIface.(*sync.Mutex)
		mu.Lock()
		defer mu.Unlock()
	}

	cmd := str.StringList(args.cmdline...)
	if xLog, ok := cfg.BuildXWriter(ctx); ok {
		text := new(strings.Builder)
		if args.dir != "" {
			text.WriteString("cd ")
			text.WriteString(args.dir)
			text.WriteString("; ")
		}
		for i, arg := range cmd {
			if i > 0 {
				text.WriteByte(' ')
			}
			switch {
			case strings.ContainsAny(arg, "'"):
				// Quote args that could be mistaken for quoted args.
				text.WriteByte('"')
				text.WriteString(bashQuoter.Replace(arg))
				text.WriteByte('"')
			case strings.ContainsAny(arg, "$`\\*?[\"\t\n\v\f\r \u0085\u00a0"):
				// Quote args that contain special characters, glob patterns, or spaces.
				text.WriteByte('\'')
				text.WriteString(arg)
				text.WriteByte('\'')
			default:
				text.WriteString(arg)
			}
		}
		fmt.Fprintf(xLog, "%s\n", text)
```

**File:** src/cmd/go/internal/generate/generate.go (L485-512)
```go
// exec runs the command specified by the argument. The first word is
// the command name itself.
func (g *Generator) exec(words []string) {
	path := words[0]
	if path != "" && !strings.Contains(path, string(os.PathSeparator)) {
		// If a generator says '//go:generate go run <blah>' it almost certainly
		// intends to use the same 'go' as 'go generate' itself.
		// Prefer to resolve the binary from GOROOT/bin, and for consistency
		// prefer to resolve any other commands there too.
		gorootBinPath, err := pathcache.LookPath(filepath.Join(cfg.GOROOTbin, path))
		if err == nil {
			path = gorootBinPath
		}
	}
	cmd := exec.Command(path, words[1:]...)
	cmd.Args[0] = words[0] // Overwrite with the original in case it was rewritten above.

	// Standard in and out of generator should be the usual.
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	// Run the command in the package directory.
	cmd.Dir = g.dir
	cmd.Env = str.StringList(cfg.OrigEnv, g.env)
	err := cmd.Run()
	if err != nil {
		g.errorf("running %q: %s", words[0], err)
	}
}
```
