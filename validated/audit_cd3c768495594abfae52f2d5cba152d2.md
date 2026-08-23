### Title
DNS-rebinding protection bypass in SCP-style URL host substitution - ([File: internal/git/gitcmd/command_resolve.go])

### Summary
`getURLAndResolveConfigForSCP` is supposed to replace the hostname of an scp-like git URL (`[user@]host:path`) with a pre-vetted `resolvedAddress` so that Git never re-resolves the original hostname at connection time. Because it searches for the first `@` in the *entire* `remoteURL` string instead of only within the host segment (the part before the first `:`), a crafted URL whose path portion contains an `@` before any `@` in the host causes the substitution logic to prepend the untouched original hostname back onto the result, so the final URL still contains the attacker-controlled hostname as the effective connection target.

### Finding Description
The function does two independent splits on the same raw `remoteURL`: [1](#0-0) 

`hostAndPath := strings.SplitN(remoteURL, ":", 2)` correctly isolates the host segment (`hostAndPath[0]`) before the first colon, and this is what determines the actual host used by Git's own scp-like URL parser. However, the user-prefix extraction uses `strings.SplitAfterN(remoteURL, "@", 2)` on the *whole* `remoteURL`, not on `hostAndPath[0]`. If the host segment contains no `@` but the path segment (after the host-delimiting colon) does contain an `@` before its own colon, `userPrefix` incorrectly captures everything up to and including that later `@` — which includes the original, unresolved hostname and its delimiting colon.

Example: `remoteURL = "gitlab.com:evil@internal-service:repo.git"`, `resolvedAddress = "203.0.113.5"`.
- `hostAndPath` = `["gitlab.com", "evil@internal-service:repo.git"]`
- `strings.SplitAfterN(remoteURL, "@", 2)` finds the `@` inside `evil@internal-service`, giving `userPrefix = "gitlab.com:evil@"`
- Result: `fmt.Sprintf("%s%s:%s", userPrefix, resolvedAddress, hostAndPath[1])` = `"gitlab.com:evil@203.0.113.5:evil@internal-service:repo.git"`

This string is then used verbatim as the git remote URL (e.g. via `remote.inmemory.url` in `buildCommandOpts`): [2](#0-1) 

Git's own scp-like URL parser splits on the *first* colon not preceded by `/` to determine `[user@]host`; for the crafted string above that is `gitlab.com` (no `@` before that first colon), so Git/SSH will connect to `gitlab.com` — the original, un-pinned hostname — and re-resolve it via DNS at fetch time, completely bypassing the `resolvedAddress` pinning that the anti-DNS-rebinding mechanism relies on. No existing check catches this: there is no validation that the returned URL's host component actually equals `resolvedAddress`, and the SCP branch's only defensive check is `strings.Contains(hostAndPath[0], "/")`, which does not guard against this case.

### Impact Explanation
This defeats the anti-DNS-rebinding protection ("Git is never re-configured by input" / resolved-address pinning) for SSH-transport fetches reached through `FetchRemote`, `CreateRepositoryFromURL`, `UpdateRemoteMirror`, and `FindRemoteRootRef`. An attacker who controls a mirror/import URL and its accompanying `ResolvedAddress` (or relies on the standard flow where Rails resolves and validates the address once, then Gitaly is expected to pin to it) can cause the outbound SSH connection to go to whatever host their DNS record for the original hostname resolves to at connection time, rather than the address that was checked as safe — a classic TOCTOU DNS-rebinding SSRF that reaches internal hosts/services from the Gitaly node.

### Likelihood Explanation
Exploitation requires only that the attacker supply an SCP-style remote URL (not `ssh://`, `http(s)://`, or `git://`) whose path component contains an `@` occurring before its own `:` — a trivially craftable, valid-looking git path (e.g. `evil@internal-service:repo.git`, or simply any ref/refspec-like path containing `@`). This is reachable through any repository-mirroring/import-by-URL feature exposed to ordinary users (pull mirrors, project import via URL) that ultimately invokes `FetchRemote`/`CreateRepositoryFromURL` with `RemoteParams.Url`/`ResolvedAddress`. No special privileges, secrets, or non-default configuration are needed.

### Recommendation
Perform the `@` search only within `hostAndPath[0]` (the already-isolated host segment), not the full `remoteURL`, e.g.:
```go
var userPrefix string
if idx := strings.Index(hostAndPath[0], "@"); idx != -1 {
    userPrefix = hostAndPath[0][:idx+1]
}
return fmt.Sprintf("%s%s:%s", userPrefix, resolvedAddress, hostAndPath[1]), nil, nil
```
Additionally, add a unit test asserting that the substituted host is always exactly `resolvedAddress` regardless of `@`/`:` characters appearing later in the path, and consider verifying post-construction that parsing the returned URL yields a host equal to `resolvedAddress`.

### Proof of Concept
```go
func TestGetURLAndResolveConfigForSCP_HostSubstitutionBypass(t *testing.T) {
    url, _, err := GetURLAndResolveConfig(
        "gitlab.com:evil@internal-service:repo.git",
        "203.0.113.5",
    )
    require.NoError(t, err)
    // Expected (correct) behavior: host fully replaced by resolvedAddress.
    // require.Equal(t, "203.0.113.5:evil@internal-service:repo.git", url)

    // Actual (buggy) behavior observed:
    require.Equal(t, "gitlab.com:evil@203.0.113.5:evil@internal-service:repo.git", url)
    // When this string is used as a git/ssh scp-like remote, Git parses the
    // host as "gitlab.com" (first colon-delimited segment), NOT "203.0.113.5",
    // demonstrating the resolved-address pinning is bypassed.
}
```

### Citations

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
