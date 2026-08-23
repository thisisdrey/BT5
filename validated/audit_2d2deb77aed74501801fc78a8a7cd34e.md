## Analysis

The external report's bug class is: **a value recovered/derived from a cryptographic verification step is trusted without checking that it actually corresponds to the expected context/identity.** The closest reachable analog in Gitaly is in the SSH commit-signature verifier, which fails to pin the signature's `namespace` field to the expected `"git"` value before trusting the verification result.

### Title
Missing SSH signature namespace validation allows signature/domain confusion in commit signature verification - ([File: internal/signature/ssh.go])

### Summary
`SSHSigningKey.Verify` parses an attacker-supplied SSH signature blob and reuses the `Namespace` field embedded in that blob itself when reconstructing the signed message, instead of asserting it equals the fixed `"git"` namespace that `CreateSignature` uses. This removes the domain-separation guarantee that SSH signature namespaces are designed to provide.

### Finding Description
`CreateSignature` always signs under the fixed constant `namespace = "git"`: [1](#0-0) [2](#0-1) .

However, `Verify` does the opposite: it decodes the untrusted signature blob and takes the `Namespace` value directly from it (`sshSig.Namespace`) rather than requiring it to equal the constant `namespace`, before calling the underlying `ssh.PublicKey.Verify`: [3](#0-2) 

There is no `if sshSig.Namespace != namespace { return error }` check anywhere in this function — the `Version` field is likewise never validated. This mirrors the reported bug class: a security-critical value produced by a cryptographic recovery/parsing step (`ecrecover`'s signer in the original report; here, the SSH signature's namespace binding) is used without validating it matches the expected value, defeating the purpose of the binding.

This `Verify` method is reachable from an ordinary, unprivileged code path: `CommitService.GetCommitSignatures`, which is driven entirely by commit IDs supplied by the RPC caller and by the (attacker-influenced) content of commits already present in the repository (e.g., pushed by any contributor): [4](#0-3) 

### Impact Explanation
Because the namespace binding of the SSH signature format (per the `PROTOCOL.sshsig` spec) exists specifically to prevent a signature made for one purpose/context from being replayed and accepted in another, its absence here breaks cross-context signature isolation for Gitaly's configured signing key. `GetCommitSignatures` uses the result of `Verify` to decide whether to label a commit's signer as `SIGNER_SYSTEM` (i.e., "signed by GitLab/Gitaly itself") versus `SIGNER_USER`: [5](#0-4) . If the same SSH key configured as `Git.SigningKey` is ever used to produce a valid signature under any namespace other than `"git"` (e.g., reused as a general-purpose SSH key elsewhere), that signature/content pair could be repurposed to make an attacker-crafted commit misreported as system-signed, spoofing the "Verified" trust indicator surfaced to GitLab users.

### Likelihood Explanation
Exploitation is not trivial: an attacker cannot forge a new signature without the private key, so they would need to already possess a legitimately-produced SSH signature (over content they control) that was generated with the same key under a different namespace, and get that exact payload into a commit body. This is a lower-likelihood scenario contingent on signing-key reuse outside the git-signing context, but it is a real, code-level gap rather than a hypothetical, and it directly matches the reported bug class of "recovered/verified identity value not checked against expected context."

### Recommendation
In `internal/signature/ssh.go`, `Verify` should explicitly reject signatures whose `sshSig.Namespace` does not equal the expected constant `namespace` (`"git"`), and should similarly validate `sshSig.Version` against the expected `version`, before proceeding to cryptographic verification:
```go
if sshSig.Namespace != namespace {
    return fmt.Errorf("unexpected signature namespace: %q", sshSig.Namespace)
}
```

### Proof of Concept
1. Configure Gitaly with `Git.SigningKey` pointing to an SSH key that is also used (or reused) to produce SSH signatures for another application/context using a namespace other than `"git"` (e.g., `ssh-keygen -Y sign -n other-namespace -f key file.txt`).
2. Obtain the resulting armored `SSH SIGNATURE` block and the exact bytes of `file.txt`.
3. As an ordinary repository contributor, craft/push a git commit whose payload (tree/parent/author/committer/message, i.e., the pre-`gpgsig` commit content) is byte-identical to `file.txt`, and attach the harvested signature block as the `gpgsig` header.
4. Call `GetCommitSignatures` for that commit ID; because `Verify` never checks that the signature's embedded namespace equals `"git"`, verification succeeds and the response labels the commit `Signer: SIGNER_SYSTEM`, even though the signature was never produced for this git commit context. [6](#0-5)

### Citations

**File:** internal/signature/ssh.go (L43-46)
```go
const (
	version          = 1
	namespace        = "git"
	hashAlgorithm    = "sha512"
```

**File:** internal/signature/ssh.go (L71-76)
```go
	signedData := signedData{
		MagicHeader:   magicHeader,
		Namespace:     namespace,
		HashAlgorithm: hashAlgorithm,
		Hash:          h.Sum(nil),
	}
```

**File:** internal/signature/ssh.go (L112-142)
```go
// Verify method verifies whether a signature has been created by this signing key
func (sk *SSHSigningKey) Verify(signatureText, signedText []byte) error {
	block, rest := pem.Decode(signatureText)
	if block == nil || len(rest) > 0 || block.Type != sshSignatureType {
		return fmt.Errorf("invalid signature text")
	}

	sshSig := &sshSignature{}
	if err := ssh.Unmarshal(block.Bytes, sshSig); err != nil {
		return fmt.Errorf("parse signature text: %w", err)
	}

	signature := &ssh.Signature{}
	if err := ssh.Unmarshal(sshSig.Signature, signature); err != nil {
		return fmt.Errorf("parse signature: %w", err)
	}

	h := sha512.New()
	if _, err := h.Write(signedText); err != nil {
		return fmt.Errorf("failed to create sha for verifying content: %w", err)
	}

	signedData := signedData{
		MagicHeader:   sshSig.MagicHeader,
		Namespace:     sshSig.Namespace,
		HashAlgorithm: sshSig.HashAlgorithm,
		Hash:          h.Sum(nil),
	}

	return sk.PrivateKey.PublicKey().Verify(ssh.Marshal(signedData), signature)
}
```

**File:** internal/gitaly/service/commit/get_commit_signatures.go (L59-76)
```go
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
```
