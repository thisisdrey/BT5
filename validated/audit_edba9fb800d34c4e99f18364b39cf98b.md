The report describes an unwhitelisted, arbitrary-destination "call" made by a privileged proxy contract. The strongest structurally analogous issue in this Gitaly codebase is in the `RemoteService.FindRemoteRepository` RPC, which passes a completely unvalidated, attacker-controlled string directly as the target of a `git ls-remote` invocation with no scheme/protocol whitelist.

### Title
Unrestricted remote URL passed to `git ls-remote` allows arbitrary command execution via Git's `ext::` transport helper - (File: internal/gitaly/service/remote/find_remote_repository.go)

### Summary
`FindRemoteRepository` takes a caller-supplied `remote` string and feeds it directly, without any scheme or protocol validation, into a `git ls-remote <remote> HEAD` invocation [1](#0-0) . Git's remote transport layer supports an `ext::` remote helper syntax (e.g. `ext::sh -c "<command>"`) that spawns an arbitrary command as the "transport." Because Gitaly does not restrict allowed protocols (e.g. via `GIT_ALLOW_PROTOCOL`/`protocol.ext.allow`, or by whitelisting `http(s)/git/ssh` schemes as it does elsewhere for DNS-rebinding mitigation), any caller of this RPC can achieve remote command execution on the Gitaly host.

### Finding Description
The `Remote` field of `FindRemoteRepositoryRequest` is a raw string that is passed straight to `git-ls-remote(1)` as an untrusted argument: [2](#0-1) 

Nowhere in this path (or in the related `gitcmd` command construction) is there a check that the `remote` value is restricted to an allowed scheme list (`http`, `https`, `git`, `ssh`), unlike the URL-resolution helper `GetURLAndResolveConfig`, which does enumerate and restrict schemes when building `http.curloptResolve` configuration for other RPCs [3](#0-2) . A search of the codebase for protocol allow-listing constructs (`GIT_ALLOW_PROTOCOL`, `protocol.allow`, `uploadpack.allowExt`) returns no results, confirming no such guard exists anywhere in the RPC handlers that shell out to Git with user-supplied remote URLs (`FindRemoteRepository`, `FindRemoteRootRef`, `FetchRemote`, `UpdateRemoteMirror`, `CreateRepositoryFromURL`).

Git natively supports the `ext::<command>` remote helper syntax, which executes `<command>` as a subprocess to act as the transport. If an attacker supplies a `remote` value such as `ext::sh -c touch\ /tmp/pwned`, the underlying `git-ls-remote` process will execute that shell command directly on the Gitaly node — this is the direct analog of the reported bug class: a privileged code path performing an unrestricted "call" (here, a subprocess invocation via Git's transport abstraction) to an attacker-chosen destination because no whitelist of acceptable targets/protocols is enforced.

### Impact Explanation
This allows arbitrary command execution in the context of the Gitaly process, on the Gitaly storage node. Since Gitaly processes host the repository data for potentially many projects/users, this is a full compromise of the Gitaly node: an attacker could read/exfiltrate any repository data on that storage shard, tamper with objects, or pivot further into the internal network. This is a critical, unauthenticated-boundary RCE if `FindRemoteRepository` (a `STORAGE`-scoped accessor RPC that does not require the caller to already control a specific repository) is reachable by any client capable of issuing gRPC calls to Gitaly with a valid auth token — which is the same trust boundary as any other tenant/project-level API caller in a multi-tenant GitLab deployment.

### Likelihood Explanation
The RPC is a simple unary accessor call requiring only a `remote` string and a `storage_name`; it performs no repository-existence checks before executing `git ls-remote` [4](#0-3) . Any actor with access to call Gitaly's `RemoteService` (e.g., through GitLab's "check repository URL" / mirror-configuration feature, which is user-facing) can trigger this with a crafted `remote` field. No race conditions, timing, or privileged prerequisites are required — likelihood is high.

### Recommendation
Reject any remote URL/value that is not of the form `http://`, `https://`, `git://`, `ssh://`, or a valid SCP-like `user@host:path` syntax before passing it to any `git` subcommand, mirroring the scheme whitelist already implemented in `GetURLAndResolveConfig` [3](#0-2) . Additionally, set `GIT_ALLOW_PROTOCOL=http:https:git:ssh` (or the equivalent `protocol.*.allow` config) in the environment of every Git subprocess Gitaly spawns for remote operations (`FindRemoteRepository`, `FindRemoteRootRef`, `FetchRemote`, `UpdateRemoteMirror`, clone-from-URL) to defense-in-depth block `ext::`, `fd::`, and any other unexpected helper-based transports even if a scheme check is bypassed.

### Proof of Concept
1. As any client with access to call Gitaly's `RemoteService.FindRemoteRepository` RPC, send:
```
FindRemoteRepositoryRequest{
  remote: "ext::sh -c 'touch /tmp/pwned'",
  storage_name: "<any valid storage>",
}
```
2. Gitaly executes `git ls-remote 'ext::sh -c '"'"'touch /tmp/pwned'"'"'' HEAD` via `gitCmdFactory.NewWithoutRepo` [5](#0-4) .
3. Git's `ext::` transport helper invokes `sh -c 'touch /tmp/pwned'` as a subprocess on the Gitaly host, demonstrating arbitrary command execution.

### Citations

**File:** internal/gitaly/service/remote/find_remote_repository.go (L13-31)
```go
func (s *server) FindRemoteRepository(ctx context.Context, req *gitalypb.FindRemoteRepositoryRequest) (*gitalypb.FindRemoteRepositoryResponse, error) {
	if req.GetRemote() == "" {
		return nil, structerr.NewInvalidArgument("empty remote can't be checked.")
	}

	var output bytes.Buffer
	cmd, err := s.gitCmdFactory.NewWithoutRepo(ctx,
		gitcmd.Command{
			Name: "ls-remote",
			Args: []string{
				req.GetRemote(),
				"HEAD",
			},
		},
		gitcmd.WithStdout(&output),
	)
	if err != nil {
		return nil, structerr.NewInternal("error executing git command: %w", err)
	}
```

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
