### Title
XCMP inbound message decoding panics on malformed HRMP data via `defensive!` in debug builds - ([File: cumulus/pallets/xcmp-queue/src/lib.rs])

### Summary
`XcmpQueue::handle_xcmp_messages` decodes untrusted, attacker-influenced bytes arriving over the HRMP/XCMP channel from a sibling parachain. When `take_first_concatenated_xcms` fails to fully decode a page (e.g. a truncated or malformed `VersionedXcm` blob), the handler invokes `defensive!("HRMP inbound decode stream broke; page will be dropped.")`, which is a hard `panic!()` whenever the binary is compiled with `debug_assertions` enabled. This mirrors the reported bug class exactly: a crafted, malformed payload reaching a decode/processing routine that lacks a graceful-error path and instead panics, rather than being cleanly rejected.

### Finding Description
`handle_xcmp_messages` is the message-routing entrypoint for a parachain's HRMP/XCMP inbound queue, fed directly with raw bytes originating from a sibling parachain's outbound channel (untrusted from this chain's perspective, no signature or origin filtering beyond channel existence) [1](#0-0) .

For the `ConcatenatedVersionedXcm` / `ConcatenatedOpaqueVersionedXcm` formats, the code loops calling `Self::take_first_concatenated_xcms` and treats any decode failure as an unrecoverable condition: [2](#0-1) 

`defensive!` is the exact analog of the "no `recover()` / no explicit guard" pattern from the report: it does not return a typed error to the caller for graceful handling; instead it unconditionally executes `panic!()` under `debug_assertions`, and only degrades to a log line in release builds. This is directly confirmed by the pallet's own test suite, which asserts a real panic occurs in debug mode for exactly this code path: [3](#0-2) 
and that the *same* malformed input is a silent no-op only in release builds: [4](#0-3) 

The same defensive-panic-on-malformed-input pattern also guards the blob-message branch: [5](#0-4) 

Just as the Go report's `BitmapToQuorumIds` panic requires an attacker to construct a byte sequence that decodes into an internally inconsistent `big.Int` (bypassing normal construction paths), here an attacker-controlled sibling parachain (or an operator relaying crafted HRMP data) only needs to place a truncated/invalid `VersionedXcm` after a valid `ConcatenatedVersionedXcm` format tag to force the decode-stream-broke branch, e.g.:
```rust
let data = [ConcatenatedVersionedXcm.encode(), Xcm::<Test>(vec![]).encode()].concat();
```
No existing guard upstream (`XcmpMessageFormat::decode`, channel-suspension checks, weight metering) inspects the *payload* for well-formedness before the `take_first_concatenated_xcms` call; the only backstop is `defensive!`'s conditional-panic behavior, which is architecturally identical to relying on an unvalidated internal invariant instead of an explicit check.

### Impact Explanation
Where `debug_assertions` are enabled — development/test networks (Rococo/Westend-style testnets frequently run debug or `--release` with debug-assertions, CI, benchmarking harnesses, and `try-runtime`/migration dry-run tooling used ahead of on-chain runtime upgrades) — a sibling parachain (or any actor able to get a crafted HRMP page delivered, which does not require validator/collator/relayer compromise, only normal parachain-to-parachain HRMP messaging) can force a hard `panic!()` inside `on_initialize`/message processing. Because this executes inside block-authoring/import logic, a panic here degrades or halts message queue processing for that collator/node instance, matching the "public underpriced work that degrades block production or stalls bridge processing" and "runtime bugs that compromise intended behavior" impact categories. In release builds (mainnet Polkadot/Kusama collators) the panic is suppressed and downgraded to a logged/counted defensive event and the page is dropped instead — this bounds the severity for production collators to a silent message-loss condition, not a node crash.

### Likelihood Explanation
No privileged access, validator, collator, or relayer compromise is required — merely the ability to send a malformed XCMP/HRMP page as a connected sibling parachain, which is normal protocol-level interaction, not an attack on infrastructure. The exact trigger is demonstrated by the pallet's own `handle_invalid_data_panics` test. The likelihood of full impact is contingent on the target node being built with debug assertions enabled (true for CI, testnets, and pre-upgrade `try-runtime` checks, false for standard mainnet release builds), which is why this is presented as a debug-build/testnet-scoped issue rather than a universal mainnet DoS.

### Recommendation
Replace the `defensive!` panic-on-malformed-page path in `handle_xcmp_messages` (and the analogous blob-message branch) with a pure logging/metric-increment and page-drop, removing the `debug_assertions`-gated `panic!()` entirely for this externally-reachable, untrusted-input code path. `defensive!` should be reserved for invariants that are provably unreachable through any external input; since this branch is directly reachable by a sibling parachain supplying arbitrary bytes, it should be treated as expected/handled untrusted input, identical in spirit to adding the explicit `-0` guard recommended in the original report instead of relying on unchecked internal state.

### Proof of Concept
Existing repository test reproduces the panic deterministically: [6](#0-5) 
Running this test (or any debug-assertions build processing equivalent HRMP data: `[ConcatenatedVersionedXcm.encode(), Xcm::<Test>(vec![]).encode()].concat()`) causes `XcmpQueue::handle_xcmp_messages` to panic with `"HRMP inbound decode stream broke; page will be dropped."` instead of gracefully dropping the malformed page, confirmed by the counterpart release-mode test asserting a silent no-op for the identical input.

### Citations

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L958-966)
```rust
impl<T: Config> XcmpMessageHandler for Pallet<T> {
	fn handle_xcmp_messages<'a, I: Iterator<Item = (ParaId, RelayBlockNumber, &'a [u8])>>(
		iter: I,
		max_weight: Weight,
	) -> Weight {
		let mut meter = WeightMeter::with_limit(max_weight);

		let mut known_xcm_senders = BTreeSet::new();
		for (sender, _sent_at, mut data) in iter {
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L1044-1060)
```rust
					let mut can_process_next_batch = true;
					while can_process_next_batch {
						let batch = match Self::take_first_concatenated_xcms(
							&mut data,
							encoding,
							XCM_BATCH_SIZE,
							&mut meter,
						) {
							Ok(batch) => batch,
							Err(batch) => {
								can_process_next_batch = false;
								defensive!(
									"HRMP inbound decode stream broke; page will be dropped."
								);
								batch
							},
						};
```

**File:** cumulus/pallets/xcmp-queue/src/lib.rs (L1076-1079)
```rust
				XcmpMessageFormat::ConcatenatedEncodedBlob => {
					defensive!("Blob messages are unhandled - dropping");
					continue;
				},
```

**File:** cumulus/pallets/xcmp-queue/src/tests.rs (L462-472)
```rust
/// Invalid concatenated XCMs panic in debug mode.
#[test]
#[should_panic = "HRMP inbound decode stream broke; page will be dropped."]
#[cfg(debug_assertions)]
fn handle_invalid_data_panics() {
	new_test_ext().execute_with(|| {
		let data = [ConcatenatedVersionedXcm.encode(), Xcm::<Test>(vec![]).encode()].concat();

		XcmpQueue::handle_xcmp_messages(once((1000.into(), 1, data.as_slice())), Weight::MAX);
	});
}
```

**File:** cumulus/pallets/xcmp-queue/src/tests.rs (L474-486)
```rust
/// Invalid concatenated XCMs do not panic in release mode but are just a No-OP.
#[test]
#[cfg(not(debug_assertions))]
fn handle_invalid_data_no_panic() {
	new_test_ext().execute_with(|| {
		let data = [ConcatenatedVersionedXcm.encode(), Xcm::<Test>(vec![]).encode()].concat();

		frame_support::assert_storage_noop!(XcmpQueue::handle_xcmp_messages(
			once((1000.into(), 1, data.as_slice())),
			Weight::MAX,
		));
	});
}
```
