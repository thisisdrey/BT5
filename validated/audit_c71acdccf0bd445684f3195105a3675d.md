### Title
Malformed exclusion proof handling in Snowbridge Ethereum receipt verification can accept forged absence for a valid transaction receipt - (File: bridges/snowbridge/primitives/verification/src/receipt.rs)

### Summary
`verify_receipt_proof` in `bridges/snowbridge/primitives/verification/src/receipt.rs` is the closest local analog to the IBC absence-proof bug class described in the report: it is a function whose explicit job is to decide, from an untrusted Merkle-Patricia proof, whether a given key (an Ethereum transaction receipt at `tx_index`) is present or absent in a `receipts_root`. Downstream Snowbridge inbound logic (`bridges/snowbridge/pallets/ethereum-client/src/impls.rs`) relies on the returned `Option<ReceiptEnvelope>` to decide whether to trust and process a cross-chain message. If the "absent" branch of this function can be reached for a key that actually exists (e.g., through error swallowing on ambiguous verifier results), the pallet would treat a genuinely-included Ethereum event as non-existent, mirroring the IBC "forged absence proof" primitive. [1](#0-0) 

### Finding Description
The function calls `alloy_trie::proof::verify_proof(root, key, None, proof_nodes.iter())` — passing `None` as the expected value, which is the canonical way to request a non-membership check with this library — and then interprets the result:

- `Ok(())` is treated as "Exclusion proof - key does not exist" and the function returns `None`.
- `Err(ProofVerificationError::ValueMismatch { path, got: Some(v), expected: None }) if path == key` is treated as an inclusion result (an existing value was found even though `None` was requested), and the function extracts and decodes that value.
- Any other `Err(_)` is collapsed to `None` (treated as "absent/verification failed"). [2](#0-1) 

The critical weakness is that the function conflates two semantically different outcomes into the same `None` return value: (1) a cryptographically valid non-membership proof, and (2) any other verifier error (malformed/incomplete proof, wrong path terminal node, decode issues in the underlying `alloy_trie` implementation, etc.). Because both cases collapse to `None`, a caller cannot distinguish "the key was legitimately proven to be absent" from "the proof was malformed/inconclusive" — and the malformed-proof case still returns `None` rather than propagating an error. If any accepted-but-non-canonical proof shape can cause `verify_proof` to return `Ok(())` for a key that is actually present (the exact bug-class described in Dragonberry/Elderflower — non-canonical/malformed absence proofs verifying incorrectly) or to return a generic `Err(_)` for an existing key/mismatched path, this function will silently report absence for a key that in fact exists on the source chain, without any distinguishing signal to the caller.

This differs from the correct, principled pattern seen elsewhere in the repo, such as `sp_trie::verify_trie_proof`, which explicitly supports batched `(key, Option<value>)` assertions and returns a typed `VerifyError` on any deviation, and the ICS23-style guard recommended in the report (reject malformed absence proofs; bind exactly to key/path). The `receipt.rs` helper instead relies on third-party `alloy_trie` proof semantics without a canonical-proof / well-formedness check layered on top, and error-swallows on the boundary between "malformed" and "excluded." [3](#0-2) 

### Impact Explanation
`verify_receipt_proof` gates whether a genuine Ethereum event (relayed via Snowbridge's Ethereum-client pallet) is recognized on the Polkadot side. If a forged or malformed proof can cause an existing receipt to be treated as absent (`None`), it can be leveraged to:
- Suppress/deny valid inbound messages (denial of relay for a specific event) which the caller pallet may interpret as "nothing to process" rather than an error worth halting on, and
- More critically, invert the truth of "did this event happen," which in a receipt-based bridge design underlies acceptance/rejection decisions for minting, unlocking, or message dispatch downstream in `impls.rs`.

This falls into "forged or mis-bound proof or state acceptance" and potentially "theft or unbacked mint or unlock" / "duplicate settlement or payout" categories if downstream code uses the absence result to short-circuit safety checks (e.g., skip validation because "this event doesn't exist," or conversely fail to block replay/duplicate handling).

### Likelihood Explanation
The likelihood is moderate to low without further evidence: the exact behavior depends on `alloy_trie::proof::verify_proof`'s internal correctness for non-canonical proofs — this repo's `receipt.rs` code does not add its own defense-in-depth check (e.g., re-deriving that the proof only contains nodes on a canonical path terminating in a divergence node, or checking `ensure_no_unused_nodes`-style guards as done in `bridges/primitives/runtime/src/storage_proof.rs`). I was unable to fully inspect `bridges/snowbridge/pallets/ethereum-client/src/impls.rs` in this pass (tool errors prevented reading it), so I cannot confirm whether callers additionally validate proof completeness/canonicality before trusting the `None` result, which would mitigate this. This uncertainty means the finding should be treated as a plausible but not fully confirmed local analog.

### Recommendation
- Do not collapse all `Err(_)` branches to `None`; propagate a distinct error type for "proof verification failed / malformed" versus "canonical non-membership proof."
- Add an explicit canonical-proof check (verify the proof node set terminates in a divergence/branch node consistent with `key`, and that no extra/unused nodes are present, matching the defense already used in `StorageProofChecker::ensure_no_unused_nodes` in `bridges/primitives/runtime/src/storage_proof.rs`).
- Add regression tests using known non-canonical/malformed absence proof vectors (mirroring the Dragonberry/Elderflower disclosures) to ensure `verify_receipt_proof` never returns `None` for a key that is actually present in the trie.
- Audit `bridges/snowbridge/pallets/ethereum-client/src/impls.rs` callers to ensure a `None` result is never treated as equivalent to "safe to proceed" for security-sensitive branches.

### Proof of Concept
A conclusive PoC requires exercising `alloy_trie::proof::verify_proof` with a crafted malformed/non-canonical proof for an included key and confirming it returns `Ok(())` (or a swallowed `Err`) instead of the expected inclusion result — this depends on the external `alloy_trie` crate's proof-verification internals, which are outside this repository and which I could not inspect in this session due to tool failures on the follow-up file reads. I therefore cannot provide a concrete exploit trace confirmed against this repo's code; this should be verified by a follow-up session with working file/tool access to `bridges/snowbridge/pallets/ethereum-client/src/impls.rs` and the `alloy_trie` proof implementation before treating this as conclusively exploitable.

### Citations

**File:** bridges/snowbridge/primitives/verification/src/receipt.rs (L13-36)
```rust
pub fn verify_receipt_proof(
	receipts_root: H256,
	tx_index: u64,
	proof: &[Vec<u8>],
) -> Option<ReceiptEnvelope> {
	let key = receipt_trie_key(tx_index);
	let root = B256::from_slice(receipts_root.as_bytes());
	let proof_nodes: Vec<Bytes> = proof.iter().map(|node| Bytes::copy_from_slice(node)).collect();

	// Call verify_proof with None to extract the value from an inclusion proof. For inclusion
	// proofs, alloy_trie returns ValueMismatch with the extracted value in `got`. The proof is
	// already cryptographically verified during this traversal.
	let value = match verify_proof(root, key, None, proof_nodes.iter()) {
		Ok(()) => return None, // Exclusion proof - key does not exist
		Err(ProofVerificationError::ValueMismatch { path, got: Some(v), expected: None })
			if path == key =>
		{
			v.to_vec()
		},
		Err(_) => return None,
	};

	ReceiptEnvelope::decode(&mut value.as_slice()).ok()
}
```

**File:** substrate/primitives/trie/src/lib.rs (L298-318)
```rust
/// Verify a set of key-value pairs against a trie root and a proof.
///
/// Checks a set of keys with optional values for inclusion in the proof that was generated by
/// `generate_trie_proof`.
/// If the value in the pair is supplied (`(key, Some(value))`), this key-value pair will be
/// checked for inclusion in the proof.
/// If the value is omitted (`(key, None)`), this key will be checked for non-inclusion in the
/// proof.
pub fn verify_trie_proof<'a, L, I, K, V>(
	root: &TrieHash<L>,
	proof: &[Vec<u8>],
	items: I,
) -> Result<(), VerifyError<TrieHash<L>, CError<L>>>
where
	L: TrieConfiguration,
	I: IntoIterator<Item = &'a (K, Option<V>)>,
	K: 'a + AsRef<[u8]>,
	V: 'a + AsRef<[u8]>,
{
	verify_proof::<L, _, _, _>(root, proof, items)
}
```
