### Title
Non-atomic commit of Snowbridge outbound message leaves vs. nonce/reward accounting - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core defect is that a "Hub"-side accounting update (debt/credit totals) is written unconditionally when a message is dispatched to a remote execution layer ("Spoke"), without any atomicity/liquidity check tying the accounting write to the actual remote outcome — enabling divergence between the two states and repeatable exploitation via message retries. The closest local analog is in Snowbridge's outbound queue v2 pallet, where a message's *irreversible* commitment into the Ethereum-bound Merkle root (`Messages`/`MessageLeaves`) is written before, and is explicitly documented as **not atomically coupled** to, the pallet's own bookkeeping (`Nonce<T>`, `PendingOrders<T>`) that gates relayer reward settlement.

### Finding Description
`Pallet::do_process_message` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` is annotated with an explicit maintainer warning: [1](#0-0) 

The function then performs four sequential, individually-fallible-adjacent storage writes:
1. `Messages::<T>::append(outbound_message)` — the message content that will be delivered to Ethereum.
2. `MessageLeaves::<T>::append(message_abi_encoded_hash)` — the leaf that is folded into the Merkle root.
3. `PendingOrders::<T>::insert(nonce, order)` — the record that gates relayer reward payout via `submit_delivery_receipt`.
4. `Nonce::<T>::set(nonce)` — the monotonic counter that determines the *next* message's nonce. [2](#0-1) 

Critically, `commit()` — invoked unconditionally from `on_finalize` — folds whatever is currently in `MessageLeaves` into a Merkle root and writes it into the block header digest: [3](#0-2) [4](#0-3) 

Once that root is in the header digest, the message is **permanently and irreversibly bound for execution on Ethereum** — exactly the "Hub" side committing state for a cross-domain send. However, unlike a `#[pallet::call]` dispatchable (which FRAME wraps transactionally so an `Err` reverts all writes), this function is invoked from the `MessageQueue` pallet's message-processing path via `process_message_impl.rs`, and the pallet author explicitly disclaims transactional safety for it: [5](#0-4) 

Because writes 1–2 (the irrevocable commitment artifacts) are ordered *before* writes 3–4 (the accounting artifacts that gate reward payout and next-nonce allocation), any fallible step introduced between them — by this pallet's own maintainers in a future change, or by any of the currently-adjacent operations (`OutboundCommandWrapper` bound conversion, `T::GasMeter` gas estimation, hashing) failing in a non-obvious way — leaves a message permanently committed to the Ethereum-bound Merkle root with **no corresponding `PendingOrder`/`Nonce` entry**, or with a `Nonce` value that is never advanced so that a subsequent message reuses the same nonce and **overwrites** the first message's `PendingOrder` in the map. This is structurally identical to the reported bug: the side that commits externally-visible/irreversible state (Hub chain accounting / here, the Ethereum-bound commitment) is not atomically coupled to the side that must actually settle or reconcile (Spoke chain liquidity / here, the relayer reward and nonce bookkeeping), and the pallet explicitly documents that no automatic rollback protects this coupling.

### Impact Explanation
If the two write groups diverge:
- A message can be permanently committed for execution on Ethereum (via the header-digest Merkle root) while its `PendingOrder` never exists or gets overwritten by a nonce collision, causing the legitimate relayer to be permanently unable to claim their reward (`Error::InvalidPendingNonce` in `process_delivery_receipt`) even though they performed real off-chain relaying work — this is a "permanent user/relayer-fund lock" per the impact gate.
- Nonce collisions between two distinct outbound messages break the nonce-based replay/ordering guarantee that the Ethereum Gateway relies on to accept `submit_delivery_receipt`/execute commands, which can corrupt the bridge's delivery-receipt and reward bookkeeping (duplicate-settlement risk) — matching "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Likelihood Explanation
This is a **latent, not currently demonstrated** exploit under the exact present code path: tracing `do_process_message` line-by-line, all fallible operations (`Nonce` overflow check, `BoundedVec` conversion of commands) occur *before* the first storage write (`Messages::append`), so under today's exact code there is no reachable branch that writes `Messages`/`MessageLeaves` and then fails before `PendingOrders`/`Nonce` are updated. The likelihood is therefore tied to the fragility the maintainers themselves flagged (no rollback protection) rather than a proven, currently-triggerable sequence — this should be treated as a structural weakness/regression risk rather than a confirmed live exploit, and any claim of direct exploitability today would require further live tracing (e.g., of `T::GasMeter::maximum_dispatch_gas_used_at_most` and the `OutboundCommandWrapper` bound) that could not be completed with the tools available in this session.

### Recommendation
- Wrap `do_process_message`'s four writes in an explicit `frame_support::storage::with_transaction` (or equivalent atomic block) so that any future fallible operation added between them causes a full rollback, removing reliance on manual ordering discipline.
- Alternatively, reorder the function so `PendingOrders::insert` and `Nonce::set` happen strictly before `Messages::append`/`MessageLeaves::append`, ensuring the irrevocable Ethereum-bound commitment can never occur without a valid, matching accounting record.
- Add an invariant check/test asserting `Messages.len() == MessageLeaves.len() == (count of PendingOrders inserted this block)` at `on_finalize`, to catch any future divergence before `commit()` writes the Merkle root into the header digest.

### Proof of Concept
Not independently reproducible against the current code as traced (no reachable fallible branch exists between the two write groups today). The proof-of-concept for this class of issue would require introducing (or discovering) a fallible operation between `Messages::<T>::append` (line 403) and `Nonce::<T>::set` (line 438) in `do_process_message` — e.g., a future change to `T::GasMeter` or the command-encoding path that can legitimately error — and then observing that the Merkle root committed in `on_finalize`/`commit()` includes a message whose `PendingOrders` entry is missing or has been overwritten by a nonce collision with a subsequently processed message in the same block, exactly mirroring the original report's pattern of Hub-side state committing ahead of/independently from Spoke-side settlement.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L283-286)
```rust
		fn on_finalize(_: BlockNumberFor<T>) {
			Self::commit();
		}
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L322-339)
```rust
		pub(crate) fn commit() {
			let count = MessageLeaves::<T>::decode_len().unwrap_or_default() as u64;
			if count == 0 {
				return;
			}

			// Create merkle root of messages
			let root = merkle_root::<<T as Config>::Hashing, _>(MessageLeaves::<T>::stream_iter());

			let digest_item: DigestItem = SnowbridgeDigestItem::SnowbridgeV2(root).into();

			// Insert merkle root into the header digest
			<frame_system::Pallet<T>>::deposit_log(digest_item);

			T::OnNewCommitment::on_new_commitment(root);

			Self::deposit_event(Event::MessagesCommitted { root, count });
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L341-346)
```rust
		/// Process a message delivered by the MessageQueue pallet.
		/// IMPORTANT!! This method does not roll back storage changes on error.
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L403-438)
```rust
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

			<Nonce<T>>::set(nonce);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/process_message_impl.rs (L11-28)
```rust
impl<T: Config> ProcessMessage for Pallet<T> {
	type Origin = T::AggregateMessageOrigin;
	fn process_message(
		message: &[u8],
		origin: Self::Origin,
		meter: &mut WeightMeter,
		_: &mut [u8; 32],
	) -> Result<bool, ProcessMessageError> {
		let weight = T::WeightInfo::do_process_message();
		if meter.try_consume(weight).is_err() {
			Self::deposit_event(Event::MessagePostponed {
				payload: message.to_vec(),
				reason: ProcessMessageError::Overweight(weight),
			});
			return Err(ProcessMessageError::Overweight(weight));
		}
		Self::do_process_message(origin, message)
	}
```
