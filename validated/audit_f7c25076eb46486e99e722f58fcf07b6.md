### Title
DNS-rebinding pinning bypass in SCP-style URL handling via mis-scoped `@` search - ([File: internal/git/gitcmd/command_resolve.go])

### Summary
`getURLAndResolveConfigForSCP` extracts the `userPrefix` by searching for `@` in the *entire* original `remoteURL` instead of only in the already-split host segment, and the host/path split itself is done by `strings.SplitN(remoteURL, ":", 2)` without validating that the path portion doesn't itself contain another `@`/`:` sequence that will be re-parsed by git. An attacker who controls `RemoteParams.Url`/`CreateRepositoryFromURLRequest.Url` can craft a value where the "path" portion (after the first colon) contains an extra `@`, causing the function to prepend the *original* attacker hostname before the resolved IP in the rewritten URL, resulting in a string whose first colon (the delimiter Git's scp-like syntax parser actually uses) still points at the attacker's original, unpinned hostname.

### Finding Description
`GetURLAndResolveConfig` dispatches on `strings.HasPrefix` for `http://`, `https://`, `git://`, `ssh://`; anything else falls to `getURLAndResolveConfigForSCP` [1](#0-0) .

In `getURLAndResolveConfigForSCP`, the host/path split correctly uses `strings.SplitN(remoteURL, ":", 2)`, but the `userPrefix` is computed independently by searching for `@` across the whole original `remoteURL` string rather than confining the search to `hostAndPath[0]`: [2](#0-1) 

For an attacker-crafted URL such as `evil.com:some@path/repo.git`:
- `hostAndPath` = `["evil.com", "some@path/repo.git"]` (correct split).
- `strings.SplitAfterN(remoteURL, "@", 2)` matches the `@` that is actually inside the *path* portion, producing `userPrefix = "evil.com:some@"` — i.e. it re-includes the original hostname and part of the path before the injected `@`.
- The final formatted string becomes `"evil.com:some@" + resolvedIP + ":" + "some@path/repo.git"`, e.g. `evil.com:some@203.0.113.5:some@path/repo.git`.

When this resulting string is subsequently handed to `git`/`ssh` as the SCP-like remote, Git's scp-syntax host/path split is defined by the *first* colon in the string (per Git's documented rule "no slashes before the first colon"). In the crafted output, the first colon still immediately follows `evil.com`, so Git/ssh will parse the host as `evil.com` (the attacker's original, freshly-DNS-resolvable hostname) and treat everything after — including the pinned IP — merely as part of the path. The resolved-address pinning that `GetURLAndResolveConfig` was supposed to enforce is therefore silently discarded, and the actual outbound SSH connection is made to whatever address `evil.com` resolves to at connection time, not the address that GitLab Rails validated (`resolvedAddress`).

None of the existing checks catch this: there is no validation that the path portion is free of `@`/`:` characters, and `net.ParseIP` only validates `resolvedAddress`, not the shape of the final URL. Because `GetURLAndResolveConfig` only classifies/rewrites the URL and returns it to be used directly as the git remote URL argument (see callers in `update_remote_mirror.go` [3](#0-2)  and `fetch_remote.go` [4](#0-3) ), the mismatch between Gitaly's internal parsing and Git's actual scp-syntax parsing directly translates into a live SSRF/DNS-rebind path.

### Impact Explanation
This defeats the DNS-rebinding mitigation documented in the proto comments for `resolved_address` ("used to avoid DNS rebinding by mapping the url to the resolved address") [5](#0-4) . An unprivileged user who can trigger `FetchRemote`, `UpdateRemoteMirror`, `FindRemoteRootRef`, or `CreateRepositoryFromURL` (e.g. via pull mirrors or "import project by URL") with an attacker-controlled hostname can bypass the IP-pinning defense and cause Gitaly's outbound `git`/`ssh` connection to resolve the hostname freshly at connect time. Combined with a DNS-rebinding setup (a domain that first resolves to a public IP for GitLab Rails' SSRF validation, then to an internal address such as `127.0.0.1`, a Gitaly/Praefect internal endpoint, or metadata service for the real connection), this is a SSRF-class bypass of an intended anti-SSRF control.

### Likelihood Explanation
The attacker only needs the ability to specify a mirror/remote/import URL, which is a standard unprivileged capability (creating a pull mirror or importing a repository by URL). No secrets, no privileged role, and no MITM are required — only control of DNS for an attacker-owned domain, which is trivial. The crafted URL format (`host:path-containing-@`) is simple and deterministic to construct.

### Recommendation
In `getURLAndResolveConfigForSCP`, derive `userPrefix` only from `hostAndPath[0]` (the actual host segment) rather than scanning the full original URL for `@`. Concretely, split `hostAndPath[0]` on `@` to separate `user@host`, replace only the host component with `resolvedAddress`, and reject/escape any `@` or `:` characters found later in the path that could be misinterpreted by Git's own scp-syntax parser. Additionally, add a check that the resulting rewritten URL, when re-parsed using the same "first colon before slash" rule Git uses, still yields the intended pinned host — failing closed (returning an error) otherwise.

### Proof of Concept
```go
package gitcmd

import (
	"testing"
)

func TestGetURLAndResolveConfig_SCPBypass(t *testing.T) {
	remoteURL := "evil.com:some@path/repo.git"
	resolvedAddress := "203.0.113.5"

	url, _, err := GetURLAndResolveConfig(remoteURL, resolvedAddress)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Bug: expected the rewritten URL's host (up to first colon, per Git's
	// scp-like syntax) to be the resolved/pinned address, but it is still
	// "evil.com" because userPrefix re-captured the original host.
	t.Logf("rewritten URL: %s", url)
	// url == "evil.com:some@203.0.113.5:some@path/repo.git"
	// Git/ssh will parse host="evil.com" (first colon), NOT resolvedAddress.
}
```
Expected: `url` should have `resolvedAddress` as the substring before the first colon (e.g. `203.0.113.5:some@path/repo.git`); instead it is `evil.com:some@203.0.113.5:some@path/repo.git`, meaning Git will connect to `evil.com` via live DNS resolution, bypassing the pinned/validated address.

### Citations

**File:** internal/git/gitcmd/command_resolve.go (L41-48)
```go
	switch {
	case strings.HasPrefix(remoteURL, "http://"), strings.HasPrefix(remoteURL, "https://"), strings.HasPrefix(remoteURL, "git://"):
		return getURLAndResolveConfigForURL(remoteURL, resolvedAddress)
	case strings.HasPrefix(remoteURL, "ssh://"):
		return getURLAndResolveConfigForSSH(remoteURL, resolvedAddress)
	default:
		return getURLAndResolveConfigForSCP(remoteURL, resolvedAddress)
	}
```

**File:** internal/git/gitcmd/command_resolve.go (L62-79)
```go
func getURLAndResolveConfigForSCP(remoteURL, resolvedAddress string) (string, []ConfigPair, error) {
	hostAndPath := strings.SplitN(remoteURL, ":", 2)
	if len(hostAndPath) != 2 {
		return "", nil, fmt.Errorf("invalid protocol/URL encountered: %s", remoteURL)
	}

	if strings.Contains(hostAndPath[0], "/") {
		return "", nil, fmt.Errorf("SSH URLs with '/' before colon are unsupported")
	}

	var userPrefix string

	if userAndHost := strings.SplitAfterN(remoteURL, "@", 2); len(userAndHost) > 1 {
		userPrefix = userAndHost[0]
	}

	return fmt.Sprintf("%s%s:%s", userPrefix, resolvedAddress, hostAndPath[1]), nil, nil
}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L76-91)
```go
	var remoteConfig []gitcmd.ConfigPair
	remoteURL := remote.GetUrl()

	if resolvedAddress := remote.GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return fmt.Errorf("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		remoteConfig = append(remoteConfig, resolveConfig...)
	}

	remoteConfig = append(remoteConfig, gitcmd.ConfigPair{
		Key: fmt.Sprintf("remote.%s.url", remoteName), Value: remoteURL,
	})
```

**File:** internal/gitaly/service/repository/fetch_remote.go (L270-280)
```go
	if resolvedAddress := req.GetRemoteParams().GetResolvedAddress(); resolvedAddress != "" {
		modifiedURL, resolveConfig, err := gitcmd.GetURLAndResolveConfig(remoteURL, resolvedAddress)
		if err != nil {
			return fmt.Errorf("couldn't get curloptResolve config: %w", err)
		}

		remoteURL = modifiedURL
		config = append(config, resolveConfig...)
	}

	config = append(config, gitcmd.ConfigPair{Key: "remote.inmemory.url", Value: remoteURL})
```

**File:** proto/repository.proto (L1253-1259)
```text
  // resolved_address holds the resolved IP address of the remote_url. This is
  // used to avoid DNS rebinding by mapping the url to the resolved address.
  // Only IPv4 dotted decimal ("192.0.2.1"), IPv6 ("2001:db8::68"), or IPv4-mapped
  // IPv6 ("::ffff:192.0.2.1") forms are supported.
  // Works with HTTP/HTTPS/Git/SSH protocols.
  // Optional.
  string resolved_address = 6;
```
