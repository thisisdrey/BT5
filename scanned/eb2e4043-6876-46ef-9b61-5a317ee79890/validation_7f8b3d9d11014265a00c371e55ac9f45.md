Based on my investigation, I found a concrete structural analog to the EigenPod issue in Snowbridge's Ethereum Beacon Client, where the fork-version configuration surface has been extended for future Ethereum hard forks but the execution-payload proof type used for actual message verification has not.

### Title
Missing `Electra`/`Fulu` execution payload header variants in `VersionedExecutionPayloadHeader` will block Snowbridge proof verification after those Ethereum hard forks - (File: `bridges/snowbridge/primitives/beacon/src/types.rs`)

### Summary
`ForkVersions` already models the `electra` and `fulu` Ethereum consensus forks [1](#0-0) , and these fork constants feed into gindex/fork-digest computations used by the beacon client (`current_sync_committee_gindex_at_slot`, `block_roots_gindex_at_slot`, etc.) inside `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`. However, the actual proof payload type that carries the Ethereum execution header for message verification, `VersionedExecutionPayloadHeader`, only defines `Capella` and `Deneb` variants [2](#0-1) . There is no `Electra` or `Fulu` variant, and no fallback/upgrade path for it in `hash_tree_root`, `block_hash`, `block_number`, or `receipts_root`, all of which exhaustively `match` only the two known variants.

### Finding Description
This is the same class of defect as the EigenPod report: a system is aware, at the configuration/metadata level, of a new protocol variant (compounding `0x02` withdrawal credentials for EigenPod; the `electra`/`fulu` fork versions for Snowbridge), but the core verification data structure that actually processes proofs was never extended to accept that variant. In EigenPod, `_podWithdrawalCredentials()` only emitted the `0x01` prefix, so validators with `0x02` credentials could never pass `_verifyWithdrawalCredentials()`. Here, `VersionedExecutionPayloadHeader` only encodes `Capella`/`Deneb` execution headers, so any inbound/outbound Ethereum message proof built from an Electra-or-later beacon block's execution payload header cannot be represented by the enum at all — a relayer cannot even construct a valid `ExecutionProof` (`bridges/snowbridge/primitives/beacon/src/types.rs:450-459`) once mainnet activates Electra, because there is no variant to put the header in, and decoding an `Electra` SCALE-encoded execution header (were one to exist) would simply fail codec decode against this enum.

Every consumer of `Verifier::verify` — `Self::verify_execution_proof(&proof.execution_proof)` in `bridges/snowbridge/pallets/ethereum-client/src/impls.rs:30` — depends transitively on `VersionedExecutionPayloadHeader` supporting the header format of whatever fork Ethereum mainnet is currently on. Existing guards (`ensure!(!Self::operating_mode().is_halted(), ...)`, merkle-branch checks, BLS signature checks) all assume a well-formed `ExecutionProof` can be constructed; none of them address the enum simply lacking a variant for the fork that is already configured in `ForkVersions`.

### Impact Explanation
If/when Ethereum mainnet activates the Electra (or later Fulu) hard fork while `ForkVersions.electra`/`ForkVersions.fulu` are already configured (which they are, in the type definition), any relayer proof built against post-Electra beacon state cannot be encoded/accepted by `snowbridge-pallet-ethereum-client`. This stalls `inbound-queue`/`inbound-queue-v2` message verification and `outbound-queue-v2` delivery-receipt verification entirely, since both paths rely on `Verifier::verify` → `verify_execution_proof`. This matches the permitted impact category of "public underpriced work that degrades block production or stalls bridge processing" — the bridge silently stops being able to relay any message that depends on execution-header proofs, with no on-chain signal other than proof-submission failures, until a runtime upgrade adds the missing variant.

### Likelihood Explanation
This is not a live exploitable path today (Electra just recently activated on Ethereum mainnet, but the enum gap is time-bound to actual fork activation and requires no attacker action — it is a systemic correctness gap, not something adversarially triggerable ahead of schedule). It is comparable in likelihood/character to the original EigenPod report: it's a real, provable gap in the codebase (config recognizes forks the data type doesn't), not a theoretical one, but its manifestation is contingent on chain-level fork timing rather than an attacker crafting malicious input.

### Recommendation
Add `Electra` (and `Fulu`, if its execution payload header format differs) variants to `VersionedExecutionPayloadHeader` in `bridges/snowbridge/primitives/beacon/src/types.rs`, implementing `hash_tree_root`, `block_hash`, `block_number`, and `receipts_root` for each new variant, mirroring how `Capella`/`Deneb` are handled. Add corresponding SSZ types under `bridges/snowbridge/primitives/beacon/src/ssz.rs` for the new fork(s), and gate acceptance/rejection of proof variants against the currently active fork so that older or newer format proofs are explicitly rejected with a clear error rather than a generic decode failure.

### Proof of Concept
Not directly demonstrable via a unit test today since Ethereum mainnet's execution-payload header schema has not changed field-for-field between Deneb and Electra (Electra's consensus changes such as EIP-7251/6110/7002 primarily affect the beacon block body and validator lifecycle, not the execution payload header schema itself). This means the concrete blocking condition depends on confirming whether a future fork changes the execution payload header fields; I could not fully verify this from the repository alone (it depends on external Ethereum consensus-spec changes, not just this codebase), so I am flagging this with reduced confidence rather than asserting exploitability with certainty. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** bridges/snowbridge/primitives/beacon/src/types.rs (L32-41)
```rust
#[derive(Clone, Encode, Decode, PartialEq, Debug, TypeInfo)]
pub struct ForkVersions {
	pub genesis: Fork,
	pub altair: Fork,
	pub bellatrix: Fork,
	pub capella: Fork,
	pub deneb: Fork,
	pub electra: Fork,
	pub fulu: Fork,
}
```

**File:** bridges/snowbridge/primitives/beacon/src/types.rs (L387-440)
```rust
pub enum VersionedExecutionPayloadHeader {
	Capella(ExecutionPayloadHeader),
	Deneb(deneb::ExecutionPayloadHeader),
}

impl VersionedExecutionPayloadHeader {
	pub fn hash_tree_root(&self) -> Result<H256, SimpleSerializeError> {
		match self {
			VersionedExecutionPayloadHeader::Capella(execution_payload_header) => {
				hash_tree_root::<SSZExecutionPayloadHeader>(
					execution_payload_header.clone().try_into()?,
				)
			},
			VersionedExecutionPayloadHeader::Deneb(execution_payload_header) => {
				hash_tree_root::<crate::ssz::deneb::SSZExecutionPayloadHeader>(
					execution_payload_header.clone().try_into()?,
				)
			},
		}
	}

	pub fn block_hash(&self) -> H256 {
		match self {
			VersionedExecutionPayloadHeader::Capella(execution_payload_header) => {
				execution_payload_header.block_hash
			},
			VersionedExecutionPayloadHeader::Deneb(execution_payload_header) => {
				execution_payload_header.block_hash
			},
		}
	}

	pub fn block_number(&self) -> u64 {
		match self {
			VersionedExecutionPayloadHeader::Capella(execution_payload_header) => {
				execution_payload_header.block_number
			},
			VersionedExecutionPayloadHeader::Deneb(execution_payload_header) => {
				execution_payload_header.block_number
			},
		}
	}

	pub fn receipts_root(&self) -> H256 {
		match self {
			VersionedExecutionPayloadHeader::Capella(execution_payload_header) => {
				execution_payload_header.receipts_root
			},
			VersionedExecutionPayloadHeader::Deneb(execution_payload_header) => {
				execution_payload_header.receipts_root
			},
		}
	}
}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L21-41)
```rust
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

		Ok(())
	}
```
