## Analog Identified

The `Forwarder.sol` bug reduces to: **a signature verification payload lacks a chain-specific binding value, so an accepted signature on one chain domain is replayable in another domain** (relayer-submitted, off-chain-signed transaction with the same nonce/account state on both domains). The closest local analog is the HOP (Hand-Off Protocol) signing payload construction in `substrate/client/hop/src/types.rs` and its on-chain re-verification described by `sp_hop::HopRuntimeApi::create_promotion_extrinsic`.

### Title
Cross-runtime/genesis signature replay in HOP submit payload due to missing chain-binding in `submit_signing_payload` - (File: `substrate/client/hop/src/types.rs`)

### Summary
`submit_signing_payload` — the function whose output the collator RPC (`hop_submit`) verifies and which the runtime pallet must re-verify during promotion — binds only a domain-context string, the data hash, and a wall-clock `submit_timestamp`. It does **not** bind any chain-specific identifier (genesis hash, spec/tx version, or runtime `ChainId`-equivalent). Any account whose `MultiSigner`/`MultiSignature` key material is reused across multiple chains sharing the HOP protocol (e.g., a parachain and its testnet, or two independent chains built from the same runtime template) can have a `(data, signer, signature, submit_timestamp)` tuple accepted by an unrelated chain's collator/runtime as long as the timestamp tolerance window has not elapsed.

### Finding Description
`substrate/client/hop/src/types.rs:299-329` defines:
```rust
pub const HOP_SUBMIT_CONTEXT: &[u8] = b"hop-submit-v1:";
pub fn submit_signing_payload(hash: &HopHash, submit_timestamp: u64) -> [u8; 32] {
    // blake2_256(HOP_SUBMIT_CONTEXT || hash || submit_timestamp)
}
``` [1](#0-0) 

This payload is verified twice:
1. At submit time in `HopRpcServer::submit` (`substrate/client/hop/src/rpc.rs:210-214`), which computes `submit_signing_payload(&hash, submit_timestamp)` and checks `multi_sig.verify(&submit_payload[..], &account_id)`. [2](#0-1) 
2. On-chain, by the runtime pallet behind `HopRuntimeApi::create_promotion_extrinsic`, which the docs state "must remain byte-identical to the pallet's `signing_payload`" and must enforce a timestamp tolerance window — but has no requirement to check chain identity. [3](#0-2) 

Unlike `pallet_verify_signature`'s `VerifySignature` extension, which hashes the *entire inherited implication* (including `frame_system::CheckGenesis` and `CheckSpecVersion` in the extension pipeline) so the payload is implicitly chain-bound, [4](#0-3)  and unlike the ERC-2612 `permit` implementation in this same repo which explicitly folds `T::ChainId::get()` into `compute_domain_separator` [5](#0-4) , the HOP submit payload has **no chain/genesis/spec-version component at all**. It is exactly the same category of defect as the audited `Forwarder.sol::_verifySig`: a signed authorization payload that omits a chain-domain separator, relying only on a data hash and short timestamp window.

### Impact Explanation
If the same `MultiSigner` keypair is authorized (`HopRuntimeApi::can_account_promote`) on two or more chains/collators running HOP-enabled nodes (e.g., production and canary/testnet deployments of the same parachain template, or a chain fork), an attacker who observes a `hop_submit` call on chain A can replay the identical `(data, recipients, signature, signer, submit_timestamp)` tuple against chain B's collator within the timestamp tolerance window. Because size/authorization checks happen before signature verification and the signature check itself passes (same public key, same payload bytes, no chain binding), the replayed submission is accepted, gets promoted via `create_promotion_extrinsic`, and consumes chain B's pool capacity/authorization budget under the victim account's name, and could also be used to force an unwanted on-chain promotion of arbitrary attacker-observed data on a chain the original signer never intended to interact with. This falls under "public underpriced work that degrades block production or stalls processing" (forced promotions/rate-limit and quota exhaustion) and "unauthorized execution" (an off-chain-authorized submission accepted on a domain it was never signed for).

### Likelihood Explanation
Exploitation requires only an unprivileged network observer capturing a `hop_submit` RPC call (data, signature, signer, timestamp are all sent over JSON-RPC, non-confidential) and replaying it verbatim to a second HOP-enabled collator before the runtime's timestamp tolerance window expires — no relayer collusion, admin action, or key compromise needed. Since `sc-hop`/`sp-hop` are new, generic, reusable crates intended to be wired into arbitrary Cumulus/omni-node deployments, multiple chains built from the same template (shared runtime binary, shared account-derivation) sharing signer keys across environments is a realistic deployment pattern, directly mirroring the audited finding's caveat about MPC-controlled deployments landing at the same address/key across chains.

### Recommendation
Bind the signing payload to a chain-specific value before hashing, analogous to `compute_domain_separator`'s inclusion of `T::ChainId`:
- Include the genesis hash and/or spec_version (obtainable from `frame_system`/`RuntimeVersion`) in `submit_signing_payload`, `HOP_CLAIM_CONTEXT`, and `HOP_ACK_CONTEXT` payloads, e.g. `blake2_256(context || genesis_hash || hash || submit_timestamp)`.
- Alternatively, route the whole submit intent through `pallet_verify_signature`/meta-tx style extensions that already embed `CheckGenesis`/`CheckSpecVersion` in the implication.
- Ensure the node-side `hop_submit`/`hop_claim`/`hop_ack` handlers and `HopRuntimeApi::create_promotion_extrinsic` re-derive the exact same chain-bound payload.

### Proof of Concept
1. Deploy two HOP-enabled chains, A and B, from the same runtime template with an account `X` authorized on both via `can_account_promote`.
2. `X` calls `hop_submit(data, recipients, signature, signer, submit_timestamp)` against chain A's collator; `signature` is computed over `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp)` — this payload contains no reference to chain A.
3. An observer of the RPC traffic (or the relaying infrastructure) forwards the identical `(data, recipients, signature, signer, submit_timestamp)` tuple to chain B's `hop_submit` within the runtime's timestamp tolerance window.
4. Chain B's `HopRpcServer::submit` re-computes the same context-only payload [6](#0-5)  and `multi_sig.verify` succeeds because nothing in the signed bytes differs between A and B; the entry is accepted into chain B's pool and later promoted on-chain via `create_promotion_extrinsic`, consuming `X`'s authorization/quota on chain B without `X`'s consent for that specific chain.

### Citations

**File:** substrate/client/hop/src/types.rs (L298-329)
```rust
/// Domain-separator prefix for `hop_submit` signatures.
pub const HOP_SUBMIT_CONTEXT: &[u8] = b"hop-submit-v1:";

/// Domain-separator prefix for `hop_claim` signatures.
pub const HOP_CLAIM_CONTEXT: &[u8] = b"hop-claim-v1:";

/// Domain-separator prefix for `hop_ack` signatures.
pub const HOP_ACK_CONTEXT: &[u8] = b"hop-ack-v1:";

/// Compute the 32-byte payload that HOP recipients / submitters sign for a given
/// operation. This is `blake2_256(context || hash)` and ensures signatures from
/// one operation cannot be replayed in another.
pub fn signing_payload(context: &[u8], hash: &HopHash) -> [u8; 32] {
	let mut buf = Vec::with_capacity(context.len() + 32);
	buf.extend_from_slice(context);
	buf.extend_from_slice(hash.as_bytes());
	blake2_256(&buf)
}

/// Compute the 32-byte payload signed at `hop_submit` time.
///
/// The runtime pallet re-derives this exact byte sequence to verify the
/// signature on-chain, so the construction must remain byte-identical to the
/// pallet's `signing_payload(data, submit_timestamp)`:
/// `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())`.
pub fn submit_signing_payload(hash: &HopHash, submit_timestamp: u64) -> [u8; 32] {
	let mut buf = [0u8; HOP_SUBMIT_CONTEXT.len() + 32 + 8];
	buf[..HOP_SUBMIT_CONTEXT.len()].copy_from_slice(HOP_SUBMIT_CONTEXT);
	buf[HOP_SUBMIT_CONTEXT.len()..HOP_SUBMIT_CONTEXT.len() + 32].copy_from_slice(hash.as_bytes());
	buf[HOP_SUBMIT_CONTEXT.len() + 32..].copy_from_slice(&submit_timestamp.to_le_bytes());
	blake2_256(&buf)
}
```

**File:** substrate/client/hop/src/rpc.rs (L207-214)
```rust
		// Domain-separated payload so a submit signature cannot be replayed as claim/ack,
		// and bound to `submit_timestamp` so an old signature can't be replayed long
		// after the fact (the runtime enforces a tolerance window on the timestamp).
		let hash = H256(blake2_256(&data.0));
		let submit_payload = submit_signing_payload(&hash, submit_timestamp);
		if !multi_sig.verify(&submit_payload[..], &account_id) {
			return Err(HopError::InvalidSignature.into());
		}
```

**File:** substrate/primitives/hop/src/lib.rs (L47-57)
```rust
		/// Construct an unsigned promotion extrinsic carrying the user's submit-time
		/// (in milliseconds from the Unix epoch), signer, signature, and timestamp
		/// so the runtime pallet can verify consent on-chain.
		///
		/// `submit_timestamp` is bound into the signed payload. Implementing
		/// runtimes **must** reject promotions whose timestamp is outside a
		/// tolerance window around the current on-chain clock — otherwise the
		/// same `(data, signer, signature)` tuple can be replayed indefinitely
		/// from the collator's persisted metadata. The width of the window is a
		/// runtime policy decision (clock skew + max acceptable promotion
		/// latency); a few hours is a reasonable upper bound.
```

**File:** substrate/frame/verify-signature/src/extension.rs (L138-154)
```rust
		// Construct the payload that the signature will be validated against. The inherited
		// implication contains the encoded bytes of the call and all of the extension data of the
		// extensions that follow in the `TransactionExtension` pipeline.
		//
		// In other words:
		// - extensions that precede this extension are ignored in terms of signature validation;
		// - extensions that follow this extension are included in the payload to be signed (as if
		//   they were the entire `SignedExtension` pipeline in the traditional signed transaction
		//   model).
		//
		// The encoded bytes of the payload are then hashed using `blake2_256`.
		let msg = inherited_implication.using_encoded(blake2_256);

		// The extension was enabled, so the signature must match.
		if !signature.verify(&msg[..], account) {
			Err(InvalidTransaction::BadProof)?
		}
```

**File:** substrate/frame/assets/precompiles/src/permit.rs (L148-177)
```rust
		/// Compute the EIP-712 domain separator for a given verifying contract.
		///
		/// DOMAIN_SEPARATOR = keccak256(abi.encode(
		///   keccak256("EIP712Domain(string name,string version,uint256 chainId,address
		/// verifyingContract)"),
		///   keccak256(name),
		///   keccak256("1"),
		///   chainId,
		///   verifyingContract
		/// ))
		///
		/// The `name` parameter should be the token name per EIP-2612 specification.
		pub fn compute_domain_separator(verifying_contract: &H160, name: &[u8]) -> H256 {
			let name_hash = keccak_256(name);
			let version_hash = keccak_256(b"1");
			let chain_id = T::ChainId::get();

			// Encode: typehash || name_hash || version_hash || chainId || verifyingContract
			let mut data = Vec::with_capacity(DOMAIN_SEPARATOR_ENCODED_LEN);
			data.extend_from_slice(&DOMAIN_TYPEHASH);
			data.extend_from_slice(&name_hash);
			data.extend_from_slice(&version_hash);
			// Pad chain_id to 32 bytes (big-endian)
			data.extend_from_slice(&[0u8; 24]);
			data.extend_from_slice(&chain_id.to_be_bytes());
			// Pad address to 32 bytes
			data.extend_from_slice(&[0u8; 12]);
			data.extend_from_slice(verifying_contract.as_bytes());

			H256(keccak_256(&data))
```
