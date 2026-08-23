### Title
SSH signature namespace confusion in `SigningKey.Verify` allows cross-context signature reuse - (File: internal/signature/ssh.go)

### Summary
Gitaly's `GetCommitSignatures` RPC uses `SigningKeys.Verify` to decide whether a commit's signature was produced by Gitaly's own signing key (marking it `SIGNER_SYSTEM`, i.e. a trusted, GitLab-verified commit). The SSH-signature verification path in `internal/signature/ssh.go` fails to enforce the `git` namespace domain-separator defined by the `sshsig` format, letting a signature created for any other purpose with the same key be replayed as if it were a valid Git-namespace signature — the same class of missing domain-separation issue as `_checkSig` in the referenced Solidity report, which failed to bind signatures to contract address/chainId and thus allowed cross-deployment/fork replay.

### Finding Description
The `sshsig` spec (referenced directly in the code's own comments) includes a `namespace` field specifically to prevent a signature created for one context (e.g., `file`, `email`) from being valid in another (e.g., `git`). Gitaly's signer, `CreateSignature`, correctly hard-codes `namespace = "git"` and `hashAlgorithm = "sha512"` when producing signatures: [1](#0-0) [2](#0-1) 

However, `Verify` does not check that an incoming signature actually uses the `git` namespace and `sha512` algorithm it expects. Instead, it blindly copies `sshSig.Namespace` and `sshSig.HashAlgorithm`— fields parsed directly out of the attacker-supplied `signatureText` blob — into the struct it hashes and verifies against: [3](#0-2) 

Because the namespace/hash-algorithm fields used for verification come from the signature itself rather than from the fixed constants (`namespace`, `hashAlgorithm`) used by `CreateSignature`, any syntactically valid `sshsig` blob produced by the same private key for a *different* namespace (e.g., a signature the key holder made to authenticate an SSH login, sign a file, or sign an email) will still successfully verify here, provided the signed digest happens to match the commit payload's hash for whatever namespace/algorithm combination is embedded in the blob. This defeats exactly the protection the `namespace` field is designed to provide, mirroring the audited Solidity bug where `_checkSig` omitted a domain separator (contract address / chainId), permitting signature reuse across contexts that were supposed to be cryptographically isolated.

### Impact Explanation
`GetCommitSignatures` is exposed to any Gitaly client that can call `CommitService.GetCommitSignatures` on a repository (reachable from GitLab's web/API layer, which in turn is reachable by any user who can push or reference commits into a repository they have read access to). The `SIGNER_SYSTEM` flag returned by this RPC is what GitLab surfaces to end users as a "Verified" / GitLab-generated commit badge. If a repository can be made to contain a commit whose author crafts an `sshsig` blob (signed with Gitaly's own signing key material via any secondary namespace usage, or via a mis-scoped signature obtained through an unrelated channel) with a matching hash for the commit payload, `Verify` will incorrectly accept it, causing Gitaly to misreport an attacker-controlled commit as "SIGNER_SYSTEM"-verified. This is a data-validation/authentication-bypass style flaw: it breaks the intended trust boundary between "signed by GitLab infrastructure" and "signed by anything the signing key ever touched."

### Likelihood Explanation
Exploitation requires the attacker to possess (or induce production of) a valid `sshsig`-format signature made with Gitaly's configured signing key for some namespace other than `git`, then craft a commit payload whose SHA-512 hash matches that of the signed blob under the attacker-chosen namespace/hash-algorithm fields. This is non-trivial (it needs either a leaked/reused signature or a hash-preimage-style construction), which is why this class of bug is rated High difficulty in the original report as well. It does not require any privileged Gitaly access — only the ability to get a crafted commit signature into a repository and to invoke the already-exposed `GetCommitSignatures` RPC, which is standard, unprivileged read functionality.

### Recommendation
In `internal/signature/ssh.go`'s `Verify`, do not trust `sshSig.Namespace` or `sshSig.HashAlgorithm` from the parsed signature blob. Instead, validate that they exactly equal the fixed constants (`namespace = "git"`, `hashAlgorithm = "sha512"`) used by `CreateSignature`, rejecting the signature outright if they differ — mirroring how `ssh-keygen -Y verify -n git` enforces the namespace parameter rather than trusting an attacker-supplied namespace field. This restores the domain separation the `sshsig` format was designed to provide, analogous to adding contract-address/chainId binding in `_checkSig`.

### Proof of Concept
1. Configure Gitaly with an SSH signing key (`git.SigningKey`).
2. Using the same private key, produce an `sshsig` blob for an arbitrary non-`git` namespace (e.g., `ssh-keygen -Y sign -n file -f key payload.bin`) whose signed SHA-512 hash equals the hash of some target `commit.SignatureData.Payload`.
3. Insert this signature into a commit object as its GPG/SSH signature field, referencing the crafted payload.
4. Call `CommitService.GetCommitSignatures` for that commit; observe `Verify` (in `internal/signature/ssh.go`) accepts the cross-namespace signature because it re-derives the verification struct from the attacker-controlled `sshSig.Namespace`/`HashAlgorithm` fields rather than enforcing the fixed `git`/`sha512` constants, causing the response to report `SIGNER_SYSTEM` for an unauthorized commit.

### Citations

**File:** internal/signature/ssh.go (L43-48)
```go
const (
	version          = 1
	namespace        = "git"
	hashAlgorithm    = "sha512"
	sshSignatureType = "SSH SIGNATURE"
)
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

**File:** internal/signature/ssh.go (L112-141)
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
```
