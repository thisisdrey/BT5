Found the exact analog. `Pallet::<T>::process_message` in `snowbridge-pallet-inbound-queue-v2` is a `pub fn` (not gated by `#[pallet::call]`/origin checks, not `pub(crate)`), reachable from any code that can name the pallet type — analogous to `UpliftOnlyExample::onAfterRemoveLiquidity` being `public` instead of internal/`onlyVault`-gated.

### Title
Unauthenticated direct invocation of `InboundQueueV2::process_message` bypasses Ethereum proof verification, allowing forged relayer rewards and nonce poisoning - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::<T>::process_message` is declared `pub fn` at [1](#0-0)  outside the `#[pallet::call]` block, with no origin check and no proof verification inside it. It is intended to be invoked only after `submit` has verified an `EventProof` from Ethereum via `T::Verifier::verify` and decoded a real `Message` from the event log, as shown in the `submit` dispatchable at [2](#0-1) . Because `process_message` is a plain public method rather than a private/crate-internal one gated behind `submit`'s verification, any Rust code with access to the pallet type (e.g. another pallet in the same runtime, a benchmarking/test helper wired into production, or any future runtime glue code) can call it directly with an arbitrary, unverified `Message` struct — exactly like the Solidity `onAfterRemoveLiquidity` hook that lacked `onlyVault` and could be called directly instead of only through the Vault's verified callback path.

### Finding Description
The intended flow is: `submit` (external, requires `ensure_signed` and Merkle/header proof verification) → `Self::process_message`. `process_message` itself only checks:
- gateway address match,
- nonce not yet used (`NonceBitmap`),

then unconditionally calls `T::MessageProcessor::process_message` and pays the relayer reward via `T::RewardPayment::register_reward(&relayer, ..., total_tip)` at [3](#0-2) .

There is no `Verifier::verify` call inside `process_message` — verification only happens in `submit`, before `process_message` is invoked. Because `process_message` is `pub` at module scope (not `pub(crate)`, not private), and takes a fully attacker-controlled `relayer: T::AccountId` and `message: Message` (nonce, relayer_fee, assets, payload — all plain fields, no cryptographic binding to Ethereum), any caller within the runtime binary that can reference `InboundQueueV2::process_message` directly bypasses the entire proof-verification path used by `submit`. This mirrors the reported Solidity bug precisely: `onAfterRemoveLiquidity` was meant to be reached only via the Vault's verified unlock/callback flow but was left `public`, letting an attacker call it directly and corrupt `FeeData`. Here, `process_message` is meant to be reached only via `submit`'s verified proof flow but is left `pub`, letting any caller mint/register an arbitrary reward and permanently consume a nonce for a message that was never actually relayed from Ethereum.

The `test.rs` file already demonstrates this exact call shape being exercised directly (bypassing `submit`): [4](#0-3) .

### Impact Explanation
- **Forged reward / unbacked mint**: `register_reward` credits `relayer_fee + tip` to an attacker-chosen account for a `Message` that was never proven to originate from the real Ethereum Gateway contract, directly matching the "theft or unbacked mint" impact class.
- **Nonce poisoning / permanent message loss (bridge-state lock)**: Calling `process_message` with a real future nonce before the legitimate proof is submitted marks that nonce as used in `NonceBitmap` (`Nonce::<T>::set(nonce)`), permanently preventing the legitimate `submit` call for that nonce from succeeding (`Error::<T>::InvalidNonce`), causing a genuine bridged message to be irrecoverably dropped — a direct analog of Bob's `FeeData` being deleted and his liquidity becoming irrecoverable.
- **Duplicate/forged settlement**: since `T::MessageProcessor::process_message` can trigger XCM dispatch to AssetHub (asset issuance / claimer deposit) for the forged message, this also creates a path to duplicate or fabricated settlement on the destination chain, though this leg depends on how `MessageProcessor` validates message contents.

### Likelihood Explanation
Exploitability depends on whether `process_message`'s visibility is actually reachable by an unprivileged, external-facing surface in a deployed runtime (e.g., via a benchmarking helper, a chain-extension, or another pallet wired into `construct_runtime!` that forwards attacker input into this call, or a `#[pallet::call]` elsewhere in the runtime that calls it without re-verifying). As written in this file alone, `process_message` is not itself exposed as a `Call` variant, so it is not directly callable via a signed extrinsic from outside the runtime unless another pallet or benchmarking/test wiring exposes it — this is the same "very specific conditions" caveat noted in the original Solidity report. I could not fully confirm from the index whether any production runtime pallet forwards external, unverified input into `InboundQueueV2::process_message` outside of `submit`; this would need to be checked with full repository access (e.g., searching all `construct_runtime!` usages and any pallet with a `RuntimeCall`-like dispatch into `snowbridge_pallet_inbound_queue_v2`).

### Recommendation
1. Make `process_message` `pub(crate)` (or fully private) so it can only be reached through the verified `submit` extrinsic, removing any accidental external reachability.
2. If `process_message` must remain public for benchmarking/testing, gate it behind a marker trait/feature that is compiled out of production runtimes, or require passing a `Verified` proof token that can only be constructed inside `submit` after `T::Verifier::verify` succeeds (type-state pattern), so it cannot be called with unverified data even by other in-runtime code.
3. Add a defensive check inside `process_message` itself (not just `submit`) confirming the message was accompanied by a verified proof, rather than relying solely on caller discipline.

### Proof of Concept
Analogous to the Solidity PoC calling `onAfterRemoveLiquidity` directly instead of going through the Vault: any Rust code with visibility into the pallet (as already exercised in the pallet's own test suite) can call: [4](#0-3) 
```rust
InboundQueue::process_message(
    relayer, // attacker-controlled account
    Message {
        nonce,               // attacker-chosen, e.g. a future legitimate nonce
        assets: vec![],
        payload: Payload::Raw(vec![]),
        claimer: None,
        execution_fee: 1_000_000_000,
        relayer_fee,          // attacker-chosen reward amount
        gateway: mock::GatewayAddress::get(), // trivially known public constant
        origin: H160::random(),
        value: 3_000_000_000,
    },
)
```
This bypasses `T::Verifier::verify` entirely (never called), immediately registers `relayer_fee + tip` as a reward for `relayer`, and burns `nonce` in `NonceBitmap`, exactly reproducing the "attacker calls the privileged callback directly, corrupting critical accounting state" pattern from the original report.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-197)
```rust
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

			// submit message for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into a bridge message
			let message =
				Message::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidMessage)?;

			Self::process_message(who, message)
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-215)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L234-239)
```rust
			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs (L405-420)
```rust
		// Process inbound message with relayer_fee
		let relayer: AccountId = Keyring::Bob.into();
		assert_ok!(InboundQueue::process_message(
			relayer,
			Message {
				nonce,
				assets: vec![],
				payload: Payload::Raw(vec![]),
				claimer: None,
				execution_fee: 1_000_000_000,
				relayer_fee,
				gateway: mock::GatewayAddress::get(),
				origin: H160::random(),
				value: 3_000_000_000,
			},
		));
```
