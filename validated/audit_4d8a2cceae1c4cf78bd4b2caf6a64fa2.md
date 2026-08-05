The code precisely matches the claim: `submit_signing_payload` in `substrate/client/hop/src/types.rs:323-329` constructs the signed payload as `blake2_256(HOP_SUBMIT_CONTEXT || hash || submit_timestamp)` with no chain-specific component, and this exact payload is verified in `HopRpcServer::submit` at `substrate/client/hop/src/rpc.rs:210-214`. By contrast, `pallet_verify_signature`'s extension hashes the full inherited implication (which includes `CheckGenesis`/`CheckSpecVersion`), and the EIP-2612-style `compute_domain_separator` in `substrate/frame/assets/precompiles/src/permit.rs:148-177` explicitly folds in `T::ChainId::get()`. HOP has neither.Confirmed: the only occurrence of `genesis_hash` in the entire HOP client crate is in the `chain_info.best_hash`/`genesis_hash` field of the mock test's `Info` struct [1](#0-0) , not in the signing-payload construction itself, and `substrate/primitives/hop/src/lib.rs` contains no chain-identity references at all. This corroborates the claim that no genesis hash, spec version, or chain ID is folded into the HOP signing payload.

Audit Report

## Title
Cross-runtime/genesis signature replay in HOP submit payload due to missing chain-binding in `submit_signing_payload` - (File: `substrate/client/hop/src/types.rs`)

## Summary
`submit_signing_payload` binds only a static domain-context string, the data hash, and a wall-clock `submit_timestamp`, with no chain-specific identifier (genesis hash, spec/tx version, or equivalent) folded into the signed bytes. [2](#0-1)  A signature produced for `hop_submit` on one chain is therefore byte-identical, and thus replayable, on any other HOP-enabled chain where the same key is authorized.

## Finding Description
`submit_signing_payload` computes `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())`. [3](#0-2)  This exact payload is re-derived and checked in `HopRpcServer::submit`, which validates `multi_sig.verify(&submit_payload[..], &account_id)` after only an authorization lookup via `can_account_promote`. [4](#0-3)  The runtime API doc for `create_promotion_extrinsic` explicitly documents the timestamp-tolerance requirement but says nothing about chain-identity binding, and the `HopRuntimeApi` trait itself has no genesis/spec-version parameter. [5](#0-4) 

Grep confirms no `genesis_hash`, `spec_version`, or `ChainId` value is incorporated anywhere in the primitives crate, and the only such reference in the client crate is an unrelated mock test field, not part of the signing construction. This is a materially different situation from two other signature schemes in the same repository: `pallet_verify_signature`'s extension hashes the full inherited implication, which transitively includes `frame_system::CheckGenesis`/`CheckSpecVersion` further down the extension pipeline, making that scheme implicitly chain-bound [6](#0-5) , and the EIP-2612-style permit precompile, which explicitly folds `T::ChainId::get()` into `compute_domain_separator`. [7](#0-6)  HOP's construction has neither mechanism.

## Impact Explanation
If the same `MultiSigner` key is authorized via `can_account_promote` on two or more HOP-enabled chains sharing the same runtime template (a realistic pattern for parachain/testnet pairs or forked deployments), a `(data, recipients, signature, signer, submit_timestamp)` tuple submitted on chain A is valid, byte-for-byte, on chain B. Because the RPC only checks data size and authorization before signature verification, and the signature check itself passes identically on both chains, a replayed submission is accepted into chain B's pool and later promoted on-chain via `create_promotion_extrinsic`, consuming chain B's pool capacity, rate-limit budget, and per-account quota under the victim's identity, and forcing an unwanted on-chain promotion the signer never consented to on that chain. This matches the "unauthorized execution" and "public underpriced work that degrades block production" impact categories in the gate.

## Likelihood Explanation
Exploitation requires only passive observation of a `hop_submit` JSON-RPC call (all fields are transmitted in the clear, non-confidential) and forwarding the identical tuple to a second HOP-enabled collator within the runtime's timestamp-tolerance window; no key compromise, relayer collusion, or privileged access is needed. The precondition — the same account/key legitimately authorized across multiple deployments of the same runtime template — is a realistic and foreseeable HOP deployment pattern given that `sc-hop`/`sp-hop` are generic, reusable crates intended for arbitrary Cumulus/omni-node chains.

## Recommendation
Fold a chain-specific value into the signed payload before hashing, analogous to `compute_domain_separator`'s inclusion of `T::ChainId`:
- Include the genesis hash and/or spec_version in `submit_signing_payload`, `HOP_CLAIM_CONTEXT`, and `HOP_ACK_CONTEXT` payload construction, e.g. `blake2_256(context || genesis_hash || hash || submit_timestamp)`.
- Alternatively, route submit intent through `pallet_verify_signature`/meta-tx-style extensions that already embed `CheckGenesis`/`CheckSpecVersion` in the inherited implication.
- Ensure `hop_submit`/`hop_claim`/`hop_ack` handlers and the runtime's `create_promotion_extrinsic` implementation re-derive the identical chain-bound payload so cross-chain signatures no longer verify.

## Proof of Concept
1. Deploy two HOP-enabled chains A and B from the same runtime template, with account `X` authorized on both via `can_account_promote`.
2. `X` signs `blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp)` — computed the same way regardless of chain — and calls `hop_submit` on chain A; `HopRpcServer::submit` accepts it. [8](#0-7) 
3. Replay the identical `(data, recipients, signature, signer, submit_timestamp)` tuple against chain B's `hop_submit` within the timestamp tolerance window; chain B independently recomputes the same context-only payload and `multi_sig.verify` succeeds because nothing in the signed bytes differs between A and B.
4. The entry is accepted into chain B's pool and is later promoted on-chain via `create_promotion_extrinsic`, consuming `X`'s authorization/quota on chain B without `X`'s consent for that chain — demonstrable as a unit test that calls `submit_sig`/`rpc.submit` with two `MockClient` instances configured with distinct (but irrelevant, since unbound) chain state and observing identical payload bytes and successful verification in both cases. [9](#0-8)

### Citations

**File:** substrate/client/hop/src/rpc.rs (L150-219)
```rust
	fn submit(
		&self,
		data: Bytes,
		recipients: Vec<Bytes>,
		signature: Bytes,
		signer: Bytes,
		submit_timestamp: u64,
	) -> RpcResult<SubmitResult> {
		let recipient_keys: RecipientVec = recipients
			.into_iter()
			.map(|r| {
				MultiSigner::decode(&mut &r.0[..])
					.map(|signer| Recipient { signer, claimed: false })
					.map_err(|_| HopError::InvalidRecipientKey)
			})
			.collect::<Result<Vec<_>, _>>()?
			.try_into()
			.map_err(|v: Vec<Recipient>| HopError::TooManyRecipients {
				provided: v.len(),
				limit: MAX_RECIPIENTS as usize,
			})?;

		let signer =
			MultiSigner::decode(&mut &signer.0[..]).map_err(|_| HopError::InvalidSigner)?;
		let multi_sig = MultiSignature::decode(&mut &signature.0[..])
			.map_err(|_| HopError::InvalidSignature)?;

		let chain_info = self.client.info();
		let best_hash = chain_info.best_hash;

		let data_len = data.0.len();

		// Reject oversized payloads before the per-account authorization lookup so
		// a flood of too-big submits cannot force runtime state reads. The cap is
		// the runtime-declared `max_promotion_size`; the runtime is authoritative.
		let runtime_max = runtime_api::max_promotion_size::<Block, _>(&*self.client, best_hash)
			.map_err(HopError::from)?;
		if data_len > runtime_max as usize {
			return Err(HopError::DataTooLarge(data_len, runtime_max).into());
		}

		// Check authorization before verifying the signature: a flood of unauthorized
		// requests must not force a signature verification per submit.
		// `can_account_promote` returns false for any reason the runtime rejects:
		// unauthorized account or exhausted per-account quota.
		let account_id: AccountId32 = signer.clone().into_account();
		let authorized = runtime_api::can_account_promote::<Block, _>(
			&*self.client,
			best_hash,
			account_id.clone(),
			data_len as u32,
		)
		.map_err(HopError::from)?;
		if !authorized {
			return Err(HopError::NotAuthorized.into());
		}

		// Domain-separated payload so a submit signature cannot be replayed as claim/ack,
		// and bound to `submit_timestamp` so an old signature can't be replayed long
		// after the fact (the runtime enforces a tolerance window on the timestamp).
		let hash = H256(blake2_256(&data.0));
		let submit_payload = submit_signing_payload(&hash, submit_timestamp);
		if !multi_sig.verify(&submit_payload[..], &account_id) {
			return Err(HopError::InvalidSignature.into());
		}

		let sender_id: [u8; 32] = account_id.into();
		self.pool
			.insert(data.0, recipient_keys, sender_id, signer, multi_sig, submit_timestamp)?;
		Ok(SubmitResult { pool_status: self.pool.status() })
```

**File:** substrate/client/hop/src/rpc.rs (L372-378)
```rust
	/// Produce a domain-separated submit signature for `data` bound to a timestamp.
	fn submit_sig(pair: &ed25519::Pair, data: &[u8], submit_timestamp: u64) -> Bytes {
		let hash = H256(blake2_256(data));
		let payload = submit_signing_payload(&hash, submit_timestamp);
		let multi_sig = MultiSignature::Ed25519(pair.sign(&payload));
		Bytes(multi_sig.encode())
	}
```

**File:** substrate/client/hop/src/types.rs (L317-329)
```rust
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

**File:** substrate/primitives/hop/src/lib.rs (L47-63)
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
		fn create_promotion_extrinsic(
			data: alloc::vec::Vec<u8>,
			signer: sp_runtime::MultiSigner,
			signature: sp_runtime::MultiSignature,
			submit_timestamp: u64,
		) -> Block::Extrinsic;
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
