### Title
Cross-chain / cross-runtime replay of `hop_submit` authorization signature due to missing chain-binding in `submit_signing_payload` - (File: substrate/client/hop/src/types.rs)

### Summary
The HOP subsystem lets a signed-off-chain "submit" authorization (`MultiSignature` over `submit_signing_payload`) be captured off-chain and later replayed on-chain by an unsigned promotion extrinsic that a runtime pallet re-verifies (`HopEntryMeta.signature`/`.signer`/`.submit_timestamp`, re-derived from `HopPromoter::promote`). The signed payload binds only to `HOP_SUBMIT_CONTEXT`, `blake2_256(data)`, and `submit_timestamp` — it does **not** bind to genesis hash, spec version, or any chain/runtime identifier, unlike the standard FRAME extrinsic signing scheme (`CheckGenesis`, `CheckSpecVersion`, etc.) used everywhere else in the codebase.

### Finding Description
`submit_signing_payload` in [1](#0-0)  computes the signed digest purely as:

```
blake2_256(HOP_SUBMIT_CONTEXT || blake2_256(data) || submit_timestamp.to_le_bytes())
```

This is the exact payload an end user signs off-chain via `MultiSigner`/`MultiSignature` when calling the `hop_submit` RPC in [2](#0-1) . Compare this to the standard on-chain transaction-signing scheme used elsewhere in the same repo, where `frame_system::CheckGenesis` explicitly binds the genesis hash into the `Implicit` signed data specifically "to provide replay protection between different networks" [3](#0-2) , and `pallet_meta_tx`/`pallet_verify_signature` explicitly compose `CheckGenesis` into every signed extension tuple for the same reason [4](#0-3) .

The HOP submit signature, per its own doc comment, is carried forward and "re-verified by the runtime pallet when the unsigned promotion extrinsic lands on-chain": *"The runtime pallet re-verifies the submit signature using this key when the unsigned promotion extrinsic lands on-chain"* [5](#0-4) . The maintenance task later builds this unsigned promotion extrinsic and submits it via `HopPromoter::promote(data, signer, signature, submit_timestamp)` [6](#0-5) , and the runtime API constructs the actual extrinsic from these raw components [7](#0-6) .

Because `AccountId32`/`MultiSigner` key material and SS58 addresses are chain-agnostic, and because the runtime does not derive `HopRuntimeApi::can_account_promote` authorization from anything chain-specific in the signed payload itself, a `(data, signer, signature, submit_timestamp)` tuple captured off one HOP-enabled chain (e.g., a testnet, a fork, or any other chain sharing the runtime's account format and within the timestamp tolerance window) is byte-identical and independently valid on any other HOP-enabled chain where the same account happens to be authorized. This mirrors exactly the Harpie `changeRecipientAddress()` bug class: a signature intended to authorize an action on one network can be captured and replayed verbatim on a different network because `chain.id` (here: genesis hash / spec version) was never part of the signed material.

### Impact Explanation
If the same operator/account key is authorized to promote data on more than one HOP-enabled chain (e.g., staging and production networks sharing a keyring, or a chain fork/rollback scenario), an attacker who observes a `hop_submit` call on chain A can replay the identical `(data, signer, signature, submit_timestamp)` tuple against chain B's `hop_submit` RPC. This lets the attacker force unauthorized promotion of arbitrary attacker-supplied `data` onto chain B under someone else's authorized identity, consuming that account's per-account promotion quota (`can_account_promote`), polluting the on-chain-store, and creating on-chain state the impersonated signer never intended to attest to on that specific chain — a direct analog of "false state acceptance" / unauthorized origin action from a replayed cross-network signature.

### Likelihood Explanation
The only guard against replay is the `submit_timestamp` tolerance window enforced by the runtime, which bounds *time* but not *chain identity*. No `chain.id`/genesis-hash/spec-version check exists anywhere in `signing_payload`/`submit_signing_payload` [8](#0-7) . Any environment where the same signer key is valid/authorized on more than one deployment of the HOP-enabled runtime (test networks, staging vs production, chain migrations/hard forks sharing genesis-independent authorization state) is immediately exploitable without needing a malicious validator, relayer, or admin — purely a public RPC caller replaying previously observed public data.

### Recommendation
Bind a chain/runtime identifier into the signed payload, mirroring `frame_system::CheckGenesis`: include the genesis hash (and ideally spec version) inside `submit_signing_payload` (and `signing_payload` for claim/ack), e.g. `blake2_256(HOP_SUBMIT_CONTEXT || genesis_hash || blake2_256(data) || submit_timestamp.to_le_bytes())`, and have both the RPC-side verification and the on-chain re-verification in the promotion pallet derive/require this genesis hash so a signature from one chain cannot decode/verify on another.

### Proof of Concept
1. Chain A and Chain B both run the HOP-enabled runtime and both list `AccountId32(K)` as authorized in `can_account_promote` (e.g., shared operator key across a staging/production pair, or K is authorized after a network fork/relaunch).
2. On Chain A, the operator calls `hop_submit(data, recipients, signature, signer=K, submit_timestamp=T)` where `signature = Sign_K(submit_signing_payload(blake2_256(data), T))` per [9](#0-8) .
3. An observer captures the public RPC call parameters (`data`, `signature`, `signer`, `T`) from Chain A within the timestamp tolerance window.
4. The observer replays the identical parameters to Chain B's `hop_submit` RPC. Because `submit_signing_payload` never included any chain-specific value, `multi_sig.verify(&submit_payload[..], &account_id)` in [10](#0-9)  succeeds identically on Chain B, and the entry is accepted into Chain B's pool and later promoted on-chain under K's identity — despite K never intending to submit data to Chain B.

### Citations

**File:** substrate/client/hop/src/types.rs (L73-85)
```rust
	/// `MultiSigner` of the account that signed the submission. The runtime pallet
	/// re-verifies the submit signature using this key when the unsigned promotion
	/// extrinsic lands on-chain.
	pub signer: MultiSigner,
	/// The user's `hop_submit` signature over `submit_signing_payload(blake2_256(data),
	/// submit_timestamp)`. Carried along for the runtime to re-verify; "submit implies
	/// consent to promote" is the protocol semantic.
	pub signature: MultiSignature,
	/// Submit-time wall-clock timestamp (ms since unix epoch) bound into the
	/// signing payload. The runtime rejects promotions whose timestamp is too far
	/// from on-chain time, so old `(data, signer, signature)` tuples cannot be
	/// replayed indefinitely.
	pub submit_timestamp: u64,
```

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

**File:** substrate/frame/system/src/extensions/check_genesis.rs (L27-60)
```rust
/// Genesis hash check to provide replay protection between different networks.
///
/// # Transaction Validity
///
/// Note that while a transaction with invalid `genesis_hash` will fail to be decoded,
/// the extension does not affect any other fields of `TransactionValidity` directly.
#[derive(Encode, Decode, DecodeWithMemTracking, Clone, Eq, PartialEq, TypeInfo)]
#[scale_info(skip_type_params(T))]
pub struct CheckGenesis<T: Config + Send + Sync>(core::marker::PhantomData<T>);

impl<T: Config + Send + Sync> core::fmt::Debug for CheckGenesis<T> {
	#[cfg(feature = "std")]
	fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
		write!(f, "CheckGenesis")
	}

	#[cfg(not(feature = "std"))]
	fn fmt(&self, _: &mut core::fmt::Formatter) -> core::fmt::Result {
		Ok(())
	}
}

impl<T: Config + Send + Sync> CheckGenesis<T> {
	/// Creates new `TransactionExtension` to check genesis hash.
	pub fn new() -> Self {
		Self(core::marker::PhantomData)
	}
}

impl<T: Config + Send + Sync> TransactionExtension<T::RuntimeCall> for CheckGenesis<T> {
	const IDENTIFIER: &'static str = "CheckGenesis";
	type Implicit = T::Hash;
	fn implicit(&self) -> Result<Self::Implicit, TransactionValidityError> {
		Ok(<Pallet<T>>::block_hash(BlockNumberFor::<T>::zero()))
```

**File:** substrate/frame/meta-tx/src/mock.rs (L52-67)
```rust
	/// Transaction extension.
	pub type TxExtension = (pallet_verify_signature::VerifySignature<Runtime>, TxBareExtension);

	/// Transaction extension without signature information.
	///
	/// Helper type used to decode the part of the extension which should be signed.
	pub type TxBareExtension = (
		frame_system::CheckNonZeroSender<Runtime>,
		frame_system::CheckSpecVersion<Runtime>,
		frame_system::CheckTxVersion<Runtime>,
		frame_system::CheckGenesis<Runtime>,
		frame_system::CheckMortality<Runtime>,
		frame_system::CheckNonce<Runtime>,
		frame_system::CheckWeight<Runtime>,
		pallet_transaction_payment::ChargeTransactionPayment<Runtime>,
	);
```

**File:** substrate/client/hop/src/promotion.rs (L94-114)
```rust
	fn promote(
		&self,
		data: Vec<u8>,
		signer: MultiSigner,
		signature: MultiSignature,
		submit_timestamp: u64,
	) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
		let best_hash = self.client.info().best_hash;
		let ext = runtime_api::create_promotion_extrinsic::<Block, _>(
			&*self.client,
			best_hash,
			data,
			signer,
			signature,
			submit_timestamp,
		)?;
		self.tx_pool
			.submit_local(best_hash, ext)
			.map_err(|e| format!("submit_local failed: {:?}", e))?;
		Ok(())
	}
```
