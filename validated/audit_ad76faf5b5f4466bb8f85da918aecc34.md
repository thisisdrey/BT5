### Title
Unsanitized Remote URL Injected into Git Config Key Enables Authorization-Header Scope Widening / Credential Disclosure - (File: internal/gitaly/service/repository/fetch_remote.go)

### Summary
The external report describes a class of bug where a user-controlled string (`tokenSymbol`) is embedded downstream without validation, creating an injection surface. The closest reachable analog in Gitaly is `buildCommandOpts` in `internal/gitaly/service/repository/fetch_remote.go` and `updateRemoteMirror` in `internal/gitaly/service/remote/update_remote_mirror.go`, where the caller-supplied remote URL is interpolated directly into a Git configuration **key** (`http.<url>.extraHeader`) that carries a sensitive `Authorization` header, without any validation or escaping of the URL string.

### Finding Description
Both `FetchRemote` and `UpdateRemoteMirror` build an HTTP authorization config entry by formatting the *unvalidated* remote URL into the git config key: [1](#0-0) [2](#0-1) 

The resulting `gitcmd.ConfigPair` is passed to `WithConfigEnv`, which converts it directly into `GIT_CONFIG_KEY_N` / `GIT_CONFIG_VALUE_N` environment variables without ever calling the `GitConfig.Validate()` routine that is enforced for the `-c` CLI path: [3](#0-2) [4](#0-3) 

Compare this to `GitConfig.Validate()`/`GlobalArgs()`, which is the path that *does* reject malformed/dangerous keys (missing section, embedded `=`, leading/trailing dot, regex failures) but is only exercised for `WithConfig`/`-c`, not for `WithConfigEnv`: [5](#0-4) 

Because `http.<url>.*` is a Git URL-prefix-matched configuration section (as already demonstrated in Gitaly's own tests using wildcard/glob URL subsections, e.g. `http.https://*.example.com/.proxy`), an attacker who controls the `remote.url` value (e.g., a project maintainer configuring a push/pull mirror, which is a normal, unprivileged product feature reachable through ordinary GitLab UI/API flows that ultimately invoke these RPCs) can supply a URL whose "hostname" portion is unexpectedly broad or malformed. This causes the resulting `http.<url>.extraHeader` key to match a wider set of outgoing HTTP(S) requests than intended by the RPC caller, exactly like the reported bug where an unvalidated string was trusted to be well-formed downstream.

### Impact Explanation
If the config key does not scope as narrowly as the caller intends, the injected `Authorization` header (which may carry the internal Gitaly-Rails token, a Geo secret, or another sensitive credential passed via `HttpAuthorizationHeader`) can be attached to git HTTP requests made to hosts other than the one the operator intended, leading to credential disclosure to an unintended host during the fetch/mirror operation. This matches the "SSRF or credential disclosure" category called out as an acceptable, concrete impact in the validation rules.

### Likelihood Explanation
`FetchRemote` and `UpdateRemoteMirror` are both regular mutator RPCs invoked as part of standard product features (pull mirroring, push mirroring) driven by user-supplied remote URLs and are reachable without any special/administrative privilege beyond configuring a repository's remote — i.e., from an "ordinary user's crafted RPC field," as required by the validation rules. No malicious peer, MITM, or leaked-token precondition is required; only a user-controllable `remote.url`/`RemoteParams.Url` value.

### Recommendation
Validate and canonicize the remote URL (e.g., via `net/url.Parse` and normalizing to `scheme://host[:port]`) before it is used to construct the `http.<url>.extraHeader` config key, and reuse `GitConfig.Validate()` (or an equivalent check) on the config pairs produced by `WithConfigEnv`, not only on those consumed by `WithConfig`/`-c`. Consider deriving the config-key host component from the parsed `url.URL.Host` rather than the raw, attacker-influenced string.

### Proof of Concept
1. As a project maintainer, configure a pull mirror (or trigger `FetchRemote`/`UpdateRemoteMirror` directly via gRPC) with `RemoteParams.Url` (or `Remote.Url`) set to a value whose host component is crafted to match a Git URL-prefix wildcard pattern broader than the intended remote (e.g. a URL whose authority section resolves to a short/generic prefix).
2. Supply `HttpAuthorizationHeader` as usual.
3. Observe that the resulting `GIT_CONFIG_KEY_N=http.<crafted-url>.extraHeader` / `GIT_CONFIG_VALUE_N=Authorization: <token>` pair is honored by `git fetch`/`git ls-remote` for any outgoing HTTP(S) request whose URL matches the crafted prefix pattern, rather than being strictly scoped to the single intended remote host — since no format/host validation of the URL occurs before it is spliced into the config key at [1](#0-0) .

### Citations

**File:** internal/gitaly/service/repository/fetch_remote.go (L294-299)
```go
	if authHeader := req.GetRemoteParams().GetHttpAuthorizationHeader(); authHeader != "" {
		config = append(config, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", req.GetRemoteParams().GetUrl()),
			Value: "Authorization: " + authHeader,
		})
	}
```

**File:** internal/gitaly/service/remote/update_remote_mirror.go (L93-98)
```go
	if authHeader := remote.GetHttpAuthorizationHeader(); authHeader != "" {
		remoteConfig = append(remoteConfig, gitcmd.ConfigPair{
			Key:   fmt.Sprintf("http.%s.extraHeader", remote.GetUrl()),
			Value: "Authorization: " + authHeader,
		})
	}
```

**File:** internal/git/gitcmd/command_options.go (L48-61)
```go
// ConfigPairsToGitEnvironment converts the given config pairs into a set of environment variables
// that can be injected into a Git executable.
func ConfigPairsToGitEnvironment(configPairs []ConfigPair) []string {
	env := make([]string, 0, len(configPairs)*2+1)

	for i, configPair := range configPairs {
		env = append(env,
			fmt.Sprintf("GIT_CONFIG_KEY_%d=%s", i, configPair.Key),
			fmt.Sprintf("GIT_CONFIG_VALUE_%d=%s", i, configPair.Value),
		)
	}

	return append(env, fmt.Sprintf("GIT_CONFIG_COUNT=%d", len(configPairs)))
}
```

**File:** internal/git/gitcmd/command_options.go (L203-211)
```go
// WithConfigEnv adds git configuration entries to the command's environment. This should be used
// in place of `WithConfig()` in case config entries may contain secrets which shouldn't leak e.g.
// via the process's command line.
func WithConfigEnv(configPairs ...ConfigPair) CmdOpt {
	return func(_ context.Context, _ config.Cfg, _ CommandFactory, c *cmdCfg) error {
		c.env = append(c.env, ConfigPairsToGitEnvironment(configPairs)...)
		return nil
	}
}
```

**File:** internal/gitaly/config/config.go (L436-480)
```go
// Validate validates that the Git configuration conforms to a format that Git understands.
func (cfg GitConfig) Validate() error {
	// Even though redundant, this block checks for a few things up front to give better error
	// messages to the administrator in case any of the keys fails validation.
	if cfg.Key == "" {
		return cfgerror.NewValidationError(cfgerror.ErrNotSet, "key")
	}
	if strings.Contains(cfg.Key, "=") {
		return cfgerror.NewValidationError(
			fmt.Errorf(`key %q cannot contain "="`, cfg.Key),
			"key",
		)
	}
	if !strings.Contains(cfg.Key, ".") {
		return cfgerror.NewValidationError(
			fmt.Errorf("key %q must contain at least one section", cfg.Key),
			"key",
		)
	}
	if strings.HasPrefix(cfg.Key, ".") || strings.HasSuffix(cfg.Key, ".") {
		return cfgerror.NewValidationError(
			fmt.Errorf("key %q must not start or end with a dot", cfg.Key),
			"key",
		)
	}

	if !configKeyRegex.MatchString(cfg.Key) {
		return cfgerror.NewValidationError(
			fmt.Errorf("key %q failed regexp validation", cfg.Key),
			"key",
		)
	}

	return nil
}

// GlobalArgs generates a git `-c <key>=<value>` flag. Returns an error if `Validate()` fails to
// validate the config key.
func (cfg GitConfig) GlobalArgs() ([]string, error) {
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("invalid configuration key %q: %w", cfg.Key, err)
	}

	return []string{"-c", fmt.Sprintf("%s=%s", cfg.Key, cfg.Value)}, nil
}
```
