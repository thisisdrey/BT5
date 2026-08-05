### Title
`InboundLane::receive_message` rejects same-relayer messages once `max_unrewarded_relayer_entries` is reached even though the message would merge into the existing back entry and not grow the relayer set - ([File: bridges/modules/messages/src/inbound_lane.rs])

### Summary
`InboundLane::receive_message` checks `data.relayers.len() >= max_unrewarded_relayer_entries()` and rejects the message with `TooManyUnrewardedRelayers` *before* checking whether the incoming message would actually be merged into the existing last relayer entry (same relayer as the previous entry, consecutive nonce). This mirrors the KUMASwap bug pattern: a capacity guard fires unconditionally on `length == max` without checking whether the specific incoming item would actually increase the tracked set's size.

### Finding Description
`receive_message` in [1](#0-0)  performs, in order:

1. Nonce sequencing check.
2. `if data.relayers.len() as MessageNonce >= self.storage.max_unrewarded_relayer_entries() { return ReceptionResult::TooManyUnrewardedRelayers; }` [2](#0-1) 
3. Only *after* passing that check does it decide whether to extend the existing back entry or push a new one: `match data.relayers.back_mut() { Some(entry) if entry.relayer == *relayer_at_bridged_chain => { entry.messages.note_dispatched_message(); }, _ => { data.relayers.push_back(...) } }` [3](#0-2) 

The doc comment on `InboundLaneData::relayers` explicitly states: "Multiple dispatches from the same relayer are allowed" and that the vec is meant to track distinct relayer entries, implying consecutive messages from the same relayer should *not* grow the set [4](#0-3) .

The bug: once `data.relayers.len() == max_unrewarded_relayer_entries`, step 2 rejects *every* subsequent message unconditionally — including a message from the exact same relayer as the current back entry, which (per step 3's own logic) would only call `note_dispatched_message()` on the existing entry and would **not** increase `relayers.len()`. This is structurally identical to the KUMASwap flaw: `if (_coupons.length() == _maxCoupons) revert` without checking `!_coupons.contains(bond.coupon)` — here, `if (relayers.len() >= max) reject` without checking whether the new message would coalesce into `relayers.back()` for the same relayer.

### Impact Explanation
When the unrewarded-relayer set is full, a single relayer that is actively and exclusively delivering messages (the common single-relayer operational case) gets permanently blocked from delivering further messages until a reward/confirmation round clears entries, even though its message would not have grown the bounded set at all. This causes the inbound lane to spuriously stall message delivery/dispatch for legitimate, correctly-sequenced messages — a public underpriced-work/bridge-processing-stall class impact per the scope (message queue/receipt state failing to advance despite decode+dispatch conditions actually being satisfiable). This can degrade or halt cross-chain message processing on a bridge lane that is otherwise healthy, without requiring any malicious relayer, validator, or governance actor — the same relayer honestly delivering sequential messages triggers it.

### Likelihood Explanation
This requires no adversarial actor: any lane operated by a small number of relayers (or a single relayer, a supported and expected operational mode per the doc comment) can hit `max_unrewarded_relayer_entries` under normal load if confirmations lag, at which point every subsequent otherwise-valid message from that same relayer is rejected as `TooManyUnrewardedRelayers` even though it should merge into the existing entry. The condition ("this relayer is the same as the current back entry, and count is already at max due to *other* older, still-unconfirmed relayer entries") is a normal operating state, not a contrived edge case, making the likelihood of hitting spurious stalls in production bridge operation meaningful.

### Recommendation
Reorder the checks so the capacity check only fires when the incoming message would actually create a *new* relayer entry, mirroring the KUMASwap fix pattern of `length == max && !contains(item)`:

```rust
let would_extend_existing_entry = data
    .relayers
    .back()
    .map_or(false, |entry| entry.relayer == *relayer_at_bridged_chain);

if !would_extend_existing_entry
    && data.relayers.len() as MessageNonce >= self.storage.max_unrewarded_relayer_entries()
{
    return ReceptionResult::TooManyUnrewardedRelayers;
}
```

This preserves the bound on `relayers.len()` (still enforced for actually-new entries) while allowing a relayer whose message would merge into the existing back entry to proceed even when the set is at capacity.

### Proof of Concept
1. Configure a lane with `max_unrewarded_relayer_entries = N`.
2. Have `N` distinct relayers each deliver one message (nonces `1..N`), filling `data.relayers` to length `N`, each entry from a different relayer — see test setup pattern in `fails_to_receive_messages_above_unrewarded_relayer_entries_limit_per_lane` [5](#0-4) .
3. Have the relayer of the *last* entry (`relayers.back()`) submit the next correctly-sequenced message (nonce `N+1`).
4. Expected (per the "multiple dispatches from same relayer allowed" design intent): message is dispatched and merged into the existing back entry via `note_dispatched_message()`, since `relayers.len()` does not change.
5. Actual: `receive_message` returns `ReceptionResult::TooManyUnrewardedRelayers` at line 199 before ever reaching the back-entry merge logic at line 216, incorrectly blocking a message that would not have grown the bounded relayer set.

### Citations

**File:** bridges/modules/messages/src/inbound_lane.rs (L185-229)
```rust
	/// Receive new message.
	pub fn receive_message<Dispatch: MessageDispatch<LaneId = S::LaneId>>(
		&mut self,
		relayer_at_bridged_chain: &S::Relayer,
		nonce: MessageNonce,
		message_data: DispatchMessageData<Dispatch::DispatchPayload>,
	) -> ReceptionResult<Dispatch::DispatchLevelResult> {
		let mut data = self.storage.data();
		if Some(nonce) != data.last_delivered_nonce().checked_add(1) {
			return ReceptionResult::InvalidNonce;
		}

		// if there are more unrewarded relayer entries than we may accept, reject this message
		if data.relayers.len() as MessageNonce >= self.storage.max_unrewarded_relayer_entries() {
			return ReceptionResult::TooManyUnrewardedRelayers;
		}

		// if there are more unconfirmed messages than we may accept, reject this message
		let unconfirmed_messages_count = nonce.saturating_sub(data.last_confirmed_nonce);
		if unconfirmed_messages_count > self.storage.max_unconfirmed_messages() {
			return ReceptionResult::TooManyUnconfirmedMessages;
		}

		// then, dispatch message
		let dispatch_result = Dispatch::dispatch(DispatchMessage {
			key: MessageKey { lane_id: self.storage.id(), nonce },
			data: message_data,
		});

		// now let's update inbound lane storage
		match data.relayers.back_mut() {
			Some(entry) if entry.relayer == *relayer_at_bridged_chain => {
				entry.messages.note_dispatched_message();
			},
			_ => {
				data.relayers.push_back(UnrewardedRelayer {
					relayer: relayer_at_bridged_chain.clone(),
					messages: DeliveredMessages::new(nonce),
				});
			},
		};
		self.storage.set_data(data);

		ReceptionResult::Dispatched(dispatch_result)
	}
```

**File:** bridges/modules/messages/src/inbound_lane.rs (L386-409)
```rust
	#[test]
	fn fails_to_receive_messages_above_unrewarded_relayer_entries_limit_per_lane() {
		run_test(|| {
			let mut lane = active_inbound_lane::<TestRuntime, _>(test_lane_id()).unwrap();
			let max_nonce = BridgedChain::MAX_UNREWARDED_RELAYERS_IN_CONFIRMATION_TX;
			for current_nonce in 1..max_nonce + 1 {
				assert_eq!(
					lane.receive_message::<TestMessageDispatch>(
						&(TEST_RELAYER_A + current_nonce),
						current_nonce,
						inbound_message_data(REGULAR_PAYLOAD)
					),
					ReceptionResult::Dispatched(dispatch_result(0))
				);
			}
			// Fails to dispatch new message from different than latest relayer.
			assert_eq!(
				lane.receive_message::<TestMessageDispatch>(
					&(TEST_RELAYER_A + max_nonce + 1),
					max_nonce + 1,
					inbound_message_data(REGULAR_PAYLOAD)
				),
				ReceptionResult::TooManyUnrewardedRelayers,
			);
```

**File:** bridges/primitives/messages/src/lib.rs (L204-221)
```rust
	/// Identifiers of relayers and messages that they have delivered to this lane (ordered by
	/// message nonce).
	///
	/// This serves as a helper storage item, to allow the source chain to easily pay rewards
	/// to the relayers who successfully delivered messages to the target chain (inbound lane).
	///
	/// It is guaranteed to have at most N entries, where N is configured at the module level.
	/// If there are N entries in this vec, then:
	/// 1) all incoming messages are rejected if they're missing corresponding
	/// `proof-of(outbound-lane.state)`; 2) all incoming messages are rejected if
	/// `proof-of(outbound-lane.state).last_delivered_nonce` is    equal to
	/// `self.last_confirmed_nonce`. Given what is said above, all nonces in this queue are in
	/// range: `(self.last_confirmed_nonce; self.last_delivered_nonce()]`.
	///
	/// When a relayer sends a single message, both of MessageNonces are the same.
	/// When relayer sends messages in a batch, the first arg is the lowest nonce, second arg the
	/// highest nonce. Multiple dispatches from the same relayer are allowed.
	pub relayers: VecDeque<UnrewardedRelayer<RelayerId>>,
```
