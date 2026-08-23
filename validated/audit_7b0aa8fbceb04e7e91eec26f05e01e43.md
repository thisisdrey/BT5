### Title
System-signed commits lack repository/instance binding, allowing cross-repository replay of a "verified" signature - ([File: internal/gitaly/service/commit/get_commit_signatures.go])

### Summary
Gitaly's `GetCommitSignatures` RPC verifies a commit's cryptographic signature against a single, instance-wide "system" signing key configured in `s.cfg.Git.SigningKey`, and reports `SIGNER_SYSTEM` whenever the signature validates against that key. The data that is actually signed (`commit.SignatureData.Payload`) contains only the commit's own Git object content — tree, parent(s), author, committer, message — with no repository, project, or instance identifier baked in, analogous to the missing `chainId` in the Meebits `Offer` structure.

### Finding Description
`GetCommitSignatures` parses a commit object with `catfile.NewParser().ParseCommit`, extracts `SignatureData.Signatures[0]` and `SignatureData.Payload`, and validates it with a single global key: [1](#0-0) [2](#0-1) 

The signed payload construction in `ParseCommit` only ever captures the commit's own header/body bytes (`tree`, `parent`, `author`, `committer`, message) — it never mixes in the repository's relative path, `GlRepository` ID, or storage identity: [3](#0-2) [4](#0-3) 

The verification key itself is loaded once per-instance from `Config.Git.SigningKey` (with optional rotated keys), not scoped per-repository or per-tenant: [5](#0-4) [6](#0-5) 

Because a Git commit object is content-addressed and immutable, and the exact same raw object (including its embedded `gpgsig`/SSH signature trailer) can be copied verbatim between repositories on the same Gitaly/GitLab instance (e.g. via `git fetch`/`git push` of the raw object, or `UserCommitFiles`/cherry-pick-style operations that reuse an existing OID), any ordinary user who can obtain a system-signed commit object from one repository (their own, or any repository they can read) can insert that identical object into a different repository they control. `GetCommitSignatures` on the second repository will still report `Signer: SIGNER_SYSTEM`, because the signature check only validates the byte payload against the shared instance key — it never checks that the commit actually originates from, or was created for, the target repository.

### Impact Explanation
`SIGNER_SYSTEM` is meant to assert that GitLab itself generated the commit as part of a legitimate server-side action in that repository (e.g., squash/merge/web-edit commits produced with `Sign: true`, backed by `internal/signature`). Consumers of this attribution (UI "Verified"/system badges, downstream tooling, and potentially branch/merge protection policies keyed on signature verification) rely on it as an authenticity signal scoped to the repository/project it appears in. Because the signed payload carries no repository or instance-context binding, an unprivileged user can replay a system-signed commit object into an unrelated repository and have Gitaly attest it as system-verified there too, misrepresenting authorship/provenance across repositories and potentially defeating protections that trust "GitLab system verified" as an integrity signal.

### Likelihood Explanation
The precondition is only that an attacker can read one system-signed commit's raw content and push/write an identical object into a repository they control — both trivial, unprivileged Git operations reachable through ordinary push/fetch or object-write RPCs. No leaked secrets, MITM position, or privileged role is required; this fits squarely within the "cross-repository object access" category, using only ordinary user-level repository operations.

### Recommendation
Bind the signed payload (or a companion signed envelope) to repository/instance context — e.g., include the target repository's `GlRepository`/relative path or a per-repository/per-instance key/identifier as part of what is signed and re-checked in `GetCommitSignatures`, so a signature minted for one repository cannot be reinterpreted as valid attestation in another. At minimum, `Verify` in `internal/signature/signature.go` and its caller in `get_commit_signatures.go` should cross-check contextual metadata (e.g., repository identity) alongside the raw payload/signature match before returning `SIGNER_SYSTEM`.

### Proof of Concept
1. On a GitLab instance with system commit signing enabled (`Git.SigningKey` configured), perform a server-side action (e.g., `UserSquash`/`UserCommitFiles` with `Sign: true`) in Repository A to produce a system-signed commit `C` with signature `S` over payload `P`.
2. As an ordinary user with push access to Repository B (unrelated, attacker-controlled), fetch commit `C`'s raw object bytes from Repository A (via any read RPC or `git cat-file`) and write/push the identical object into Repository B (e.g., via `git push` of the raw pack containing object `C`, or equivalent internal write path).
3. Call `GetCommitSignatures` against Repository B for commit `C`: [7](#0-6) 
4. Observe the response reports `Signer: SIGNER_SYSTEM` for commit `C` in Repository B, even though it was never produced by a legitimate system action within Repository B — because verification only checks `Verify(signature, payload)` against the shared instance key with no binding to Repository B's identity.

### Citations

**File:** internal/gitaly/service/commit/get_commit_signatures.go (L17-83)
```go
func (s *server) GetCommitSignatures(request *gitalypb.GetCommitSignaturesRequest, stream gitalypb.CommitService_GetCommitSignaturesServer) error {
	ctx := stream.Context()

	if err := s.locator.ValidateRepository(stream.Context(), request.GetRepository()); err != nil {
		return err
	}

	repo := s.localRepoFactory.Build(request.GetRepository())

	objectHash, err := repo.ObjectHash(ctx)
	if err != nil {
		return fmt.Errorf("detecting object hash: %w", err)
	}

	if err := validateGetCommitSignaturesRequest(objectHash, request); err != nil {
		return structerr.NewInvalidArgument("%w", err)
	}

	objectReader, cancel, err := s.catfileCache.ObjectReaderWithoutMailmap(ctx, repo)
	if err != nil {
		return structerr.NewInternal("%w", err)
	}
	defer cancel()

	var signingKeys *signature.SigningKeys
	if s.cfg.Git.SigningKey != "" {
		signingKeys, err = signature.ParseSigningKeys(s.cfg.Git.SigningKey, s.cfg.Git.RotatedSigningKeys...)
		if err != nil {
			return fmt.Errorf("failed to parse signing key: %w", err)
		}
	}

	parser := catfile.NewParser()
	for _, commitID := range request.GetCommitIds() {
		commitObj, err := objectReader.Object(ctx, git.Revision(commitID)+"^{commit}")
		if err != nil {
			if errors.As(err, &catfile.NotFoundError{}) {
				continue
			}
			return structerr.NewInternal("%w", err)
		}

		commit, err := parser.ParseCommit(commitObj)
		if err != nil {
			return structerr.NewInternal("%w", err)
		}

		signature := []byte{}
		if len(commit.SignatureData.Signatures) > 0 {
			// While there could be potentially multiple signatures in a Git
			// commit, like Git, we only consider the first.
			signature = commit.SignatureData.Signatures[0]
		}

		signer := gitalypb.GetCommitSignaturesResponse_SIGNER_USER
		if signingKeys != nil {
			if signingKeys.Verify(signature, commit.SignatureData.Payload) == nil {
				signer = gitalypb.GetCommitSignaturesResponse_SIGNER_SYSTEM
			}
		}

		if err = sendResponse(signature, commit, signer, stream); err != nil {
			return structerr.NewInternal("%w", err)
		}
	}

	return nil
```

**File:** internal/git/catfile/parse_commit.go (L70-79)
```go
// SignatureData holds the raw data used to validate a signed commit.
type SignatureData struct {
	// Signatures refers to the signatures present in the commit. Note that
	// Git only considers the first signature when parsing commits
	Signatures [][]byte
	// Payload refers to the commit data which is signed by the signature,
	// generally this is everything apart from the signature in the commit.
	// Headers present after the signature are not considered in the payload.
	Payload []byte
}
```

**File:** internal/git/catfile/parse_commit.go (L122-157)
```go
		switch state {
		case parseCommitStateHeader:
			key, value, ok := strings.Cut(line, " ")
			if !ok {
				// TODO: Current tests allow empty commits, we might want
				// to change this behavior.
				goto loopEnd
			}

			// For headers, we trim the newline to make it easier
			// to parse.
			value = strings.TrimSuffix(value, "\n")

			switch key {
			case "parent":
				commit.ParentIds = append(commit.ParentIds, value)
			case "author":
				commit.Author = parseCommitAuthor(value)
			case "committer":
				commit.Committer = parseCommitAuthor(value)
			case "tree":
				commit.TreeId = value
			case "encoding":
				commit.Encoding = value
			case gpgSignaturePrefix, gpgSignaturePrefixSha256:
				// Since Git only considers the first signature, we only
				// capture the first signature's type.
				commit.SignatureType = git.DetectSignatureType(value)

				state = parseCommitStateSignature
				signatures = append(signatures, []byte(value+"\n"))

				goto loopEnd
			}

			payload = append(payload, []byte(line)...)
```

**File:** internal/signature/signature.go (L26-63)
```go
// ParseSigningKeys parses a list of signing keys separated by a comma and returns
// a list of GPG or SSH keys.
// Multiple signing keys are necessary to provide proper key rotation.
// The latest signing key is specified first and used for creating a signature. The
// previous signing keys go after and are used to verify a signature.
func ParseSigningKeys(primaryPath string, secondaryPaths ...string) (*SigningKeys, error) {
	primaryKey, err := parseSigningKey(primaryPath)
	if err != nil {
		return nil, err
	}

	secondaryKeys := make([]SigningKey, 0, len(secondaryPaths))
	for _, path := range secondaryPaths {
		signingKey, err := parseSigningKey(path)
		if err != nil {
			return nil, err
		}
		secondaryKeys = append(secondaryKeys, signingKey)
	}

	return &SigningKeys{
		primaryKey:    primaryKey,
		secondaryKeys: secondaryKeys,
	}, nil
}

func parseSigningKey(path string) (SigningKey, error) {
	key, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("open file: %w", err)
	}

	if bytes.HasPrefix(key, []byte("-----BEGIN OPENSSH")) {
		return parseSSHSigningKey(key)
	}

	return parseGpgSigningKey(key)
}
```

**File:** internal/signature/signature.go (L75-87)
```go
// Verify iterates over all signing keys and returns nil if any
// verification was successful. Otherwise, the last error is returned.
// Note: when Golang 1.19 is no longer supported, can be refactored using errors.Join
func (s *SigningKeys) Verify(signature, signedText []byte) error {
	var err error
	for _, signingKey := range append([]SigningKey{s.primaryKey}, s.secondaryKeys...) {
		err = signingKey.Verify(signature, signedText)
		if err == nil {
			return nil
		}
	}
	return err
}
```
