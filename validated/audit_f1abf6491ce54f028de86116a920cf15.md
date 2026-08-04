## Finding

### Title
`CallContract` command lets an unprivileged user set an arbitrary `gas` value that directly drives the on-chain remote-fee calculation, enabling underpriced Snowbridge V2 message delivery - (File: `bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs`)

### Summary
The Uniswap-fee bug in the C4 report is a case of a protocol trusting a single hardcoded parameter instead of validating/bounding a value that determines the price of externally observable, economically-relevant work. The local analog is the inverse but structurally identical failure in Snowbridge V2 outbound messaging: every other `Command` variant uses a fixed, engineer-reviewed gas constant for fee calculation, but `Command::CallContract` uses the caller-supplied `gas` field verbatim as the gas estimate that determines the remote (Ethereum-side) delivery fee.

### Finding Description
`ConstantGasMeter::maximum_dispatch_gas_used_at_most` computes the gas estimate used for fee calculation for every command type: [1](#0-0) 

All variants except `CallContract` use a hardcoded, protocol-controlled constant (`40_000`, `50_000 + initializer.maximum_required_gas` bounded by governance, `200_000`, `1_200_000`, `100_000`). `CallContract` instead returns `*gas_limit` directly — a value taken from user input.

That `gas` field originates from an XCM `Transact` instruction that any unprivileged account can submit through `pallet_xcm::execute`/`InitiateTransfer` to the Ethereum destination. The XCM converter decodes it without any bounds check: [2](#0-1) 

The `Command::CallContract` definition confirms `gas` is a plain user-controlled `u64` field with no protocol-side minimum: [3](#0-2) 

An integration test demonstrates exactly this unrestricted, user-supplied path: an ordinary `RuntimeOrigin::signed` account builds a `ContractCall::V1 { ..., gas: 40000, ... }` and submits it via `PolkadotXcm::execute`: [4](#0-3) 

This gas figure is fed straight into the fee formula (same structure as V1's `calculate_fee`, which multiplies gas by `fee_per_gas` and adds the relayer reward): [5](#0-4) 

Because nothing enforces a floor on `gas` for `CallContract`, a user can set `gas: 1` (or any near-zero value), causing the remote-fee component computed for their message to collapse to essentially just the fixed relayer `reward`, decoupled from the real cost of delivering/executing the call on Ethereum.

### Impact Explanation
Every other command type hardcodes a conservative, protocol-vetted gas cost precisely to prevent this class of mispricing (as the PRDocs for `TransferToken` gas-limit bumps show, gas estimates are deliberately curated and bumped when found insufficient — see `prdoc/stable2503-1/pr_7947.prdoc`). `CallContract` breaks that invariant by trusting attacker input for the same fee-critical parameter. This is directly analogous to the audit finding: a single unvalidated/unbounded parameter that feeds a fee-computation function produces economically wrong (here, drastically underpriced) outcomes.

The consequence is public underpriced work against the bridge: a user can submit `CallContract` messages that consume a nonce, get committed into the Merkle root (state advances irreversibly per `do_process_message`/`commit`), and pay a fee too small to compensate relayers for the actual Ethereum-side gas/reward economics. Relayers have no incentive to service such messages, so they can be left permanently undelivered even though the outbound queue has already advanced its commitment/nonce state for them — degrading and potentially stalling bridge processing for that channel, matching the "public underpriced work that degrades block production or stalls bridge processing" and "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" impact classes.

### Likelihood Explanation
Any unprivileged account holding enough local fee/DOT to construct an XCM `Transact` to Ethereum can trigger this; no relayer, validator, governance, or admin collusion is required — it only requires crafting an XCM message with a `ContractCall::V1 { gas: <tiny value>, .. }`, which is exactly the code path exercised (with a normal value) by the existing test at `snowbridge_v2_outbound.rs:634`.

### Recommendation
Enforce a protocol-defined minimum (and possibly maximum) gas bound for `Command::CallContract` in `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, or clamp/validate the user-supplied `gas` in the XCM converter (`convert.rs`) before constructing the command, so the fee calculation can never be based on an unrealistically low attacker-chosen gas figure — consistent with how every other command already uses a hardcoded, reviewed constant.

### Proof of Concept
1. Construct an XCM (as in `transact_with_agent_from_asset_hub_without_any_asset_transfer`) with `PayFees`/`InitiateTransfer` to `ethereum()` and a `Transact` instruction encoding `ContractCall::V1 { target, calldata, value: 0, gas: 1 }`.
2. Submit via `PolkadotXcm::execute` from any signed account.
3. `XcmConverter::convert` decodes this into `Command::CallContract { gas: 1, .. }` with no validation (`convert.rs:298-304`).
4. `ConstantGasMeter::maximum_dispatch_gas_used_at_most` returns `1` for this command (`message.rs:303`), which is used to compute the remote fee — reducing it to essentially just the flat reward, decoupled from real Ethereum execution cost.
5. The message is queued, assigned a nonce, and committed into the outbound Merkle root, while being effectively unprofitable/undeliverable for relayers.

**Note on verification limits:** I was not able to fully inspect `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` and `send_message_impl.rs` fee-charging/validation logic in this pass (only partial grep matches were returned before tool access ended), so I cannot confirm with certainty whether any additional minimum-fee enforcement exists downstream in the V2 pallet that might partially mitigate this. This should be verified directly in a full session before treating the PoC as final.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/converter/convert.rs (L294-304)
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
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L634-663)
```rust
		let transact_info =
			ContractCall::V1 { target: Default::default(), calldata: vec![], gas: 40000, value: 0 };

		let xcms = VersionedXcm::from(Xcm(vec![
			WithdrawAsset(assets.clone().into()),
			PayFees { asset: local_fee_asset.clone() },
			InitiateTransfer {
				destination: ethereum(),
				remote_fees: Some(AssetTransferFilter::ReserveWithdraw(Definite(
					remote_fee_asset.clone().into(),
				))),
				preserve_origin: true,
				assets: BoundedVec::new(),
				remote_xcm: Xcm(vec![
					DepositAsset { assets: Wild(AllCounted(2)), beneficiary },
					Transact {
						origin_kind: OriginKind::SovereignAccount,
						fallback_max_weight: None,
						call: transact_info.encode().into(),
					},
				]),
			},
		]));

		<AssetHubWestend as AssetHubWestendPallet>::PolkadotXcm::execute(
			RuntimeOrigin::signed(AssetHubWestendSender::get()),
			bx!(xcms),
			Weight::from(EXECUTION_WEIGHT),
		)
		.unwrap();
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-402)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
		}

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}
```
