### Title
Unbounded, attacker-supplied `gas` value in `Command::CallContract` is trusted as the dispatch-gas estimate for Snowbridge outbound queue v2 fee/weight accounting - ([File: bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs])

### Summary
The core broken invariant in the Unlock report is: a *reward/fee amount is derived from a caller-influenced "estimated" gas figure instead of from actually measured, bounded work*, letting an attacker manipulate the estimate to extract more value than the real cost. In Snowbridge's outbound queue v2 primitives, every other `Command` variant is priced with a hard-coded, benchmarked constant in `ConstantGasMeter::maximum_dispatch_gas_used_at_most`, but `Command::CallContract` instead returns the caller-supplied `gas: u64` field verbatim, with no upper bound, sanity check, or governance-set cap.

### Finding Description
`Command::CallContract` carries a fully user-controlled `gas: u64` field: [1](#0-0) 

`GasMeter::maximum_dispatch_gas_used_at_most`, which every downstream fee/weight computation relies on to bound "the maximum amount of gas a command payload will require to *dispatch*", is implemented for all other commands with fixed, benchmarked constants (e.g. `UnlockNativeToken` → `200_000`, `RegisterForeignToken` → `1_200_000`). For `CallContract` it simply echoes back the caller's own declared value with zero clamping: [2](#0-1) 

This uncapped `gas` is what gets written into the committed, on-chain outbound message that is later ABI-encoded, merkle-committed, and relayed to Ethereum: [3](#0-2) 

The same command originates from an ordinary signed XCM `Transact`, i.e. any unprivileged user can construct a `ContractCall::V1 { gas, .. }` with an arbitrary `gas` and route it through the standard v2 XCM converter/exporter path (as shown being exercised, with attacker-chosen `gas: 100_000`, in an existing emulated test) with no origin restriction on the `gas` field itself: [4](#0-3) 

By contrast, the v1 pipeline explicitly *derives* the fee charged to the sender from `GasMeter::maximum_gas_used_at_most`, so an inflated gas figure would at least be self-taxing to the sender: [5](#0-4) 

In v2, the `fee` field of the `Message` is supplied independently by the XCM sender (via `PayFees`) and stored verbatim into `PendingOrders` when the message is processed — it is not recomputed from, or checked against, the attacker-controlled `gas` value that ends up in the committed `OutboundCommandWrapper`: [6](#0-5) 

This breaks the pivot requirement that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" with correctly bound values: here the on-chain committed `gas` (which is what the Ethereum Gateway forwards to the target contract call, and what determines relayer gas-refund exposure per the module's own documented formula `Min(GasPrice, MaxFeePerGas)*GasUsed()+Reward`) is never bound to, or validated against, the `fee` actually paid by the sender.

### Impact Explanation
An unprivileged user can submit an XCM message whose `CallContract` command declares an arbitrarily large `gas` (up to `u64::MAX`) while paying an unrelated, attacker-chosen `fee`. This is committed into `Messages`/`MessageLeaves` and merkle-rooted for relay to Ethereum. Consequences:
- Relayers who deliver the message either take on unbounded/uncompensated gas risk on the Ethereum side, or systematically refuse to deliver such messages — causing them to sit unprocessed in `PendingOrders`, degrading bridge throughput ("public underpriced work that degrades block production or stalls bridge processing").
- Because the fee is not derived from or validated against the declared `gas`, the accounting invariant that message settlement must reflect actual bounded work is violated at the point of commitment, before any external verification can catch it.

### Likelihood Explanation
Likelihood is high for causing queue degradation: constructing the XCM is trivial for any account with access to `pallet-xcm::execute`/send and requires no special privilege, key, or governance action — only a self-crafted `Transact` payload encoding `ContractCall::V1` with an inflated `gas` value, exactly as already demonstrated (with a benign value) in the repository's own emulated test suite. No validator, relayer, or admin misbehavior is required, satisfying the "unprivileged attacker" requirement of the pivots.

### Recommendation
Cap `Command::CallContract`'s `gas` field in `ConstantGasMeter::maximum_dispatch_gas_used_at_most` (and/or at message-validation time in the v2 converter/exporter) to a governance-configured maximum, mirroring the fixed/benchmarked treatment given to every other command variant. Additionally, derive/validate the sender-supplied `fee` against the actual committed `gas` (as v1 does via `calculate_fee`) so that the amount paid upfront always covers the worst-case relayer compensation implied by the committed gas value, and reject messages where `fee` is insufficient relative to the declared `gas` at submission time rather than silently queuing them.

### Proof of Concept
1. As any signed account on a source parachain, submit `pallet-xcm::execute` with an XCM containing `WithdrawAsset`/`PayFees`/`InitiateTransfer` to Ethereum, embedding `Transact { call: ContractCall::V1{ target, calldata, value: 0, gas: u64::MAX }.encode() }` (structurally identical to the existing test at `snowbridge_v2_outbound_edge_case.rs:457-462`, but with `gas: u64::MAX` instead of `100_000`) and a `fee`/`PayFees` amount sized only for a cheap command.
2. The v2 converter accepts this and produces a `Command::CallContract { gas: u64::MAX, .. }`.
3. `OutboundQueue::do_process_message` calls `GasMeter::maximum_dispatch_gas_used_at_most`, which returns `u64::MAX` unmodified, and commits `OutboundCommandWrapper { gas: u64::MAX, .. }` into `Messages`/`MessageLeaves`, with `PendingOrders[nonce].fee` set to the attacker's small, unrelated `fee`.
4. The message is now permanently part of the committed merkle root with a fee/gas mismatch that no downstream component re-validates, exercising the queue-stall / underpriced-work impact.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/message.rs (L275-306)
```rust
pub trait GasMeter {
	/// Measures the maximum amount of gas a command payload will require to *dispatch*, NOT
	/// including validation & verification.
	fn maximum_dispatch_gas_used_at_most(command: &Command) -> u64;
}

/// A meter that assigns a constant amount of gas for the execution of a command
///
/// The gas figures are extracted from this report:
/// > forge test --match-path test/Gateway.t.sol --gas-report
///
/// A healthy buffer is added on top of these figures to account for:
/// * The EIP-150 63/64 rule
/// * Future EVM upgrades that may increase gas cost
pub struct ConstantGasMeter;

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L371-413)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound_edge_case.rs (L457-462)
```rust
		let arbitrary_agent_call = ContractCall::V1 {
			target: ETHEREUM_DESTINATION_ADDRESS,
			calldata: vec![0xde, 0xad, 0xbe, 0xef],
			value: 0,
			gas: 100_000,
		};
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```
