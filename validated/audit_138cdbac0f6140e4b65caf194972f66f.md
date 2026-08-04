### Title
User-controlled `CallContract.gas` in Snowbridge outbound queue v2 is unbounded and can exceed Ethereum block gas limit, permanently stalling relayer settlement and locking the relayer reward - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
An unprivileged user can send an XCM `Transact` containing a `ContractCall::V1` with an arbitrary `gas: u64` value. This value flows unchecked through `XcmConverter::convert` into `Command::CallContract{ gas, .. }`, and then through `ConstantGasMeter::maximum_dispatch_gas_used_at_most` directly into the committed `OutboundCommandWrapper.gas` / `CommandWrapper.gas` that is delivered to the Ethereum Gateway contract. No cap exists anywhere in the pipeline that limits this value to a realistic Ethereum block gas limit, mirroring the exact bug class described in the Holograph report ("An attacker can lock operator out of the pod by setting gas limit that's higher than the block gas limit of dest chain").

### Finding Description
The XCM-to-outbound-message conversion path: [1](#0-0) 

takes the `gas` field straight out of the user-supplied, XCM-`Transact`-encoded `ContractCall::V1`: [2](#0-1) 

and places it verbatim into `Command::CallContract { gas, .. }`: [3](#0-2) 

`ConstantGasMeter::maximum_dispatch_gas_used_at_most` for `CallContract` simply returns this attacker-supplied value unmodified — unlike every other command variant, which uses a hard-coded constant (`40_000`, `200_000`, `1_200_000`, etc.): [4](#0-3) 

This value is then written directly into the committed, Merkle-rooted `OutboundCommandWrapper`/`CommandWrapper.gas` field that becomes part of the message the Ethereum Gateway contract executes against: [5](#0-4) 

At no point in `do_process_message`, `XcmConverter::convert`, or `ConstantGasMeter` is `gas` checked against any upper bound (e.g., an Ethereum block gas limit constant). This is the same class of missing validation as `HolographOperator.sol`'s unbounded `gasLimit` — a user-chosen value used to size an execution budget on the destination chain, with no ceiling enforced on the source chain before commitment.

### Impact Explanation
Once the message is committed into `Messages`/`MessageLeaves` and Merkle-rooted into the header digest, the relayer must submit it to the Ethereum Gateway and pay upfront gas to execute it. If the attacker sets `gas` above what the Ethereum block gas limit allows (or simply high enough that the transaction reverts/fails on the destination), the relayer's delivery transaction will fail on Ethereum. Because settlement (`submit_delivery_receipt` → `process_delivery_receipt`) is only reachable after a successful execution/event log on Ethereum is produced and proven, a message with an unexecutable `gas` value can never be delivered successfully: [6](#0-5) 

This permanently strands the `PendingOrders` entry (and its associated `fee`) for that nonce — the relayer reward for that message can never be paid out (no other path removes/settles it), and relayers who continually attempt to relay malformed CallContract messages waste gas without compensation, degrading the incentive to service the bridge queue. This matches the "public underpriced work that degrades... stalls bridge processing" and "permanent... bridge-state lock" categories in scope.

### Likelihood Explanation
This is trivially reachable by any unprivileged XCM sender that can route a message through the Snowbridge V2 exporter with a `Transact` instruction containing a `ContractCall::V1` — no governance, admin, or validator privilege is required, only ordinary XCM execution capability (as demonstrated by the existing test harness constructing `ContractCall::V1 { gas: 100_000, .. }` via ordinary `InitiateTransfer`/`Transact`): [7](#0-6) 
Setting `gas` to `u64::MAX` or any value exceeding realistic Ethereum block gas limits costs the attacker nothing extra beyond the normal message fee, since the fee model (`calculate_fee`) scales with the declared gas but the attacker only needs to supply the corresponding (still relatively cheap) fee/ether budget to get the message committed.

### Recommendation
Enforce a hard upper bound on `Command::CallContract.gas` (and any other user-influenced gas field) at conversion time in `XcmConverter::convert` and/or in `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, rejecting or clamping messages whose declared gas exceeds a configured maximum consistent with realistic Ethereum block gas limits, analogous to how `Command::Upgrade`'s `initializer.maximum_required_gas` should also be bounded rather than added unconditionally.

### Proof of Concept
1. Attacker submits an XCM message via `PolkadotXcm::execute` (or any exporter entry point) containing `WithdrawAsset`/`PayFees`/`AliasOrigin`/`DepositAsset`/`Transact{ call: ContractCall::V1{ target, calldata, value: 0, gas: u64::MAX }.encode() }`/`SetTopic`, as constructed in the existing integration test harness.
2. `XcmConverter::convert` accepts this (per `bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs:294-305`), producing `Command::CallContract{ gas: u64::MAX, .. }`.
3. `do_process_message` commits `OutboundCommandWrapper{ gas: u64::MAX, .. }` into `Messages`/`MessageLeaves` and creates a `PendingOrder` for the nonce (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:371-436`).
4. A relayer submits the message to the Ethereum Gateway; the transaction reverts/fails because `gas` exceeds the destination block gas limit, exactly mirroring the Holograph PoC's "VM Exception ... not enough gas left" failure.
5. `submit_delivery_receipt` can never succeed for this nonce because Ethereum never emits the corresponding success event/log, so `PendingOrders[nonce]` and its `fee` remain permanently stuck.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-305)
```rust
		// Transact commands
		let transact_call = match_expression!(self.peek(), Ok(Transact { call, .. }), call);
		if let Some(transact_call) = transact_call {
			let _ = self.next();
			let transact =
				ContractCall::decode_all(&mut transact_call.clone().into_encoded().as_slice())
					.map_err(|_| TransactDecodeFailed)?;
			match transact {
				ContractCall::V1 { target, calldata, gas, value } => commands
					.push(Command::CallContract { target: target.into(), calldata, gas, value }),
			}
		}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/mod.rs (L17-31)
```rust
/// The `XCM::Transact` payload for calling arbitrary smart contracts on Ethereum.
/// On Ethereum, this call will be dispatched by the agent contract acting as a proxy
/// for the XCM origin.
#[derive(Clone, Encode, Decode, PartialEq, Debug, TypeInfo)]
pub enum ContractCall {
	V1 {
		/// Target contract address
		target: [u8; 20],
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Include ether held by the agent contract
		value: u128,
		/// Maximum gas to forward to target contract
		gas: u64,
	},
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L182-192)
```rust
	/// Call Contract on Ethereum
	CallContract {
		/// Target contract address
		target: H160,
		/// ABI-encoded calldata
		calldata: Vec<u8>,
		/// Maximum gas to forward to target contract
		gas: u64,
		/// Include ether held by agent contract
		value: u128,
	},
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L291-306)
```rust
impl GasMeter for ConstantGasMeter {
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64 {
		match command {
			Command::SetOperatingMode { .. } => 40_000,
			Command::Upgrade { initializer, .. } => {
				// total maximum gas must also include the gas used for updating the proxy before
				// the the initializer is called.
				50_000 + initializer.maximum_required_gas
			},
			Command::UnlockNativeToken { .. } => 200_000,
			Command::RegisterForeignToken { .. } => 1_200_000,
			Command::MintForeignToken { .. } => 100_000,
			Command::CallContract { gas: gas_limit, .. } => *gas_limit,
		}
	}
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-424)
```rust
			// Convert it to OutboundMessage and save into Messages storage
			let commands: Vec<OutboundCommandWrapper> = commands
				.into_iter()
				.map(|command| OutboundCommandWrapper {
					kind: command.index(),
					gas: T::GasMeter::maximum_dispatch_gas_used_at_most(&command),
					payload: command.abi_encode(),
				})
				.collect();

			let nonce = <Nonce<T>>::get().checked_add(1).ok_or_else(|| {
				Self::deposit_event(Event::MessageRejected {
					id: None,
					payload: message.to_vec(),
					error: Unsupported,
				});
				Unsupported
			})?;

			let outbound_message = OutboundMessage {
				origin,
				nonce,
				topic: id,
				commands: commands.clone().try_into().map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: Some(id),
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?,
			};
			Messages::<T>::append(outbound_message);

			// Convert it to an OutboundMessageWrapper (in ABI format), hash it using Keccak256 to
			// generate a committed hash, and store it in MessageLeaves storage which can be
			// verified on Ethereum later.
			let abi_commands: Vec<CommandWrapper> = commands
				.into_iter()
				.map(|command| CommandWrapper {
					kind: command.kind,
					gas: command.gas,
					payload: Bytes::from(command.payload),
				})
				.collect();
			let committed_message = OutboundMessageWrapper {
				origin: FixedBytes::from(origin.as_fixed_bytes()),
				nonce,
				topic: FixedBytes::from(id.as_fixed_bytes()),
				commands: abi_commands,
			};
			let message_abi_encoded_hash =
				<T as Config>::Hashing::hash(&committed_message.abi_encode());
			MessageLeaves::<T>::append(message_abi_encoded_hash);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L457-491)
```rust
		let arbitrary_agent_call = ContractCall::V1 {
			target: ETHEREUM_DESTINATION_ADDRESS,
			calldata: vec![0xde, 0xad, 0xbe, 0xef],
			value: 0,
			gas: 100_000,
		};

		let assets = vec![local_fee_asset.clone(), remote_fee_asset.clone()];
		let forged_xcm = Xcm(vec![
			WithdrawAsset(assets.into()),
			PayFees { asset: local_fee_asset },
			// Clear the origin register to None. Under the logic flaw in the XCM executor's
			// InitiateTransfer implementation (with preserve_origin: true), this causes the
			// executor to export the message without prepending any origin-altering instructions.
			// Details: https://forum.polkadot.network/t/postmortem-xcm-initiatetransfer-origin-leak/17357
			ClearOrigin,
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::truncate_from(vec![]),
				remote_xcm: Xcm(vec![
					AliasOrigin(forged_assethub_origin),
					DepositAsset { assets: Wild(AllCounted(0)), beneficiary: beneficiary() },
					Transact {
						origin_kind: OriginKind::Xcm,
						call: arbitrary_agent_call.encode().into(),
						fallback_max_weight: None,
					},
					SetTopic([9u8; 32]),
				]),
			},
		]);
```
