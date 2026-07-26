### Title
Attacker Can Permanently Grief Victim's Confidential Asset Pending-Balance Counter, Blocking All Incoming Confidential Transfers and Deposits — (`aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move`)

---

### Summary

The `confidential_asset` module tracks how many incoming transfers a user has received in `ConfidentialStore::transfers_received`. Both `confidential_transfer()` and `deposit()` unconditionally increment this counter for the recipient before checking it against `MAX_TRANSFERS_BEFORE_ROLLOVER` (65 536). An unprivileged attacker who holds any confidential balance can send 65 536 confidential transfers (each with a valid ZK proof, including zero-amount proofs) to a victim, saturating the counter. Once saturated, every subsequent attempt to send the victim a confidential transfer or deposit aborts with `E_PENDING_BALANCE_MUST_BE_ROLLED_OVER`. The victim must rollover and re-normalize (an expensive off-chain ZK-proof computation) to recover, after which the attacker can immediately repeat the attack. The victim's only permanent escape is to pause all incoming transfers, which also blocks legitimate senders.

---

### Finding Description

**Root cause — `confidential_transfer` increments the recipient counter before checking it, and no minimum-amount guard exists:**

```
// confidential_transfer(), lines 646-653
let recip_ca_store = borrow_confidential_store_mut(to, asset_type);
let new_pending_balance = add_assign_pending(&mut recip_ca_store.pending_balance, &amount);
recip_ca_store.transfers_received += 1;          // ← always incremented

assert!(
    recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
    error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
);
``` [1](#0-0) 

The same pattern appears in `deposit()`: [2](#0-1) 

`deposit()` guards against zero-amount calls with `E_POINTLESSLY_DEPOSITING_ZERO`: [3](#0-2) 

`confidential_transfer()` has **no equivalent guard**. The ZK range proof only requires the amount to lie in `[0, 2^64)`, so a proof for amount = 0 is cryptographically valid. An attacker can therefore send 65 536 zero-amount confidential transfers, spending only gas (no tokens leave their confidential balance), and saturate the victim's counter.

`MAX_TRANSFERS_BEFORE_ROLLOVER` is 65 536: [4](#0-3) 

**Victim recovery path is expensive and repeatable by the attacker:**

`rollover_pending_balance()` requires `normalized == true`: [5](#0-4) 

After rollover, `normalized` is set to `false`: [6](#0-5) 

The victim must then call `normalize_raw()` (which requires generating a fresh Bulletproof + sigma-protocol proof off-chain) before they can rollover again. The attacker can immediately re-saturate the counter after each recovery cycle.

**The only permanent defense is pausing all incoming transfers:** [7](#0-6) 

Pausing blocks the attacker but also blocks all legitimate senders, permanently degrading the victim's participation in the confidential asset protocol.

---

### Impact Explanation

- **Availability of confidential transfers**: Once the counter is saturated, every `confidential_transfer_raw` or `deposit` targeting the victim aborts. The victim cannot receive any confidential transfers or deposits until they complete a rollover + normalize cycle.
- **Repeated griefing**: The attacker can re-saturate the counter immediately after each victim recovery, creating a sustained denial-of-service against the victim's confidential asset account.
- **No fund loss**: The victim's existing confidential balance is safe; `withdraw_to` is unaffected. However, the victim's ability to *receive* funds is blocked.
- **Attacker cost**: Zero-amount transfers cost only gas (~65 APT at 0.001 APT/tx × 65 536 txs), making the attack economically viable against high-value targets.

---

### Likelihood Explanation

- Any registered confidential-asset user can trigger this against any other registered user.
- No privileged capability is required.
- On mainnet the allow-list gates which asset types are usable, but once an asset type is approved, any holder can attack any other holder of that asset type.
- The attack is fully automatable (batch-submit 65 536 transactions with pre-computed ZK proofs).

---

### Recommendation

1. **Add a minimum-amount check in `confidential_transfer()`** analogous to the one already present in `deposit()`:
   ```move
   assert!(amount != 0, error::invalid_argument(E_POINTLESSLY_TRANSFERRING_ZERO));
   ```
   This prevents zero-cost counter inflation. (Non-zero transfers still allow griefing but require the attacker to spend real tokens.)

2. **Check the counter *before* incrementing** so that a saturated counter is detected without mutating state, allowing the transaction to fail cleanly without consuming the sender's proof-verification gas.

3. **Consider a per-sender rate limit or a minimum transfer amount enforced at the ZK-proof level** to raise the economic cost of the attack proportionally to the damage inflicted.

---

### Proof of Concept

```
1. Alice registers a ConfidentialStore for asset type T and deposits 1 000 000 units.
2. Bob registers a ConfidentialStore for asset type T.
3. Alice generates 65 536 valid TransferProofs each with amount = 0
   (new_balance = old_balance, range proof for 0 ∈ [0, 2^64) is valid).
4. Alice submits 65 536 confidential_transfer_raw transactions targeting Bob.
   After each: Bob.transfers_received += 1.
5. Bob.transfers_received == 65 536 == MAX_TRANSFERS_BEFORE_ROLLOVER.
6. Any subsequent confidential_transfer_raw or deposit targeting Bob aborts with
   E_PENDING_BALANCE_MUST_BE_ROLLED_OVER (error code 6).
7. Bob calls rollover_pending_balance() → normalized = false, transfers_received = 0.
8. Bob must now call normalize_raw() (off-chain ZK proof generation required).
9. Alice immediately re-submits 65 536 zero-amount transfers.
10. Bob is back to step 6. The cycle repeats indefinitely at Alice's discretion.
```

### Citations

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L109-109)
```text
    const MAX_TRANSFERS_BEFORE_ROLLOVER: u64 = 65536;
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L462-462)
```text
        assert!(amount != 0, error::invalid_argument(E_POINTLESSLY_DEPOSITING_ZERO));
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L478-485)
```text
        add_assign_pending(&mut ca_store.pending_balance, &new_pending_u64_no_randomness(amount));
        ca_store.transfers_received += 1;

        // Make sure the depositor has "room" in their pending balance for this deposit
        assert!(
            ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
            error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L626-626)
```text
        assert!(!incoming_transfers_paused(to, asset_type), error::invalid_state(E_INCOMING_TRANSFERS_PAUSED));
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L646-653)
```text
        let recip_ca_store = borrow_confidential_store_mut(to, asset_type);
        let new_pending_balance = add_assign_pending(&mut recip_ca_store.pending_balance, &amount);
        recip_ca_store.transfers_received += 1;

        assert!(
            recip_ca_store.transfers_received <= MAX_TRANSFERS_BEFORE_ROLLOVER,
            error::invalid_state(E_PENDING_BALANCE_MUST_BE_ROLLED_OVER)
        );
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L777-778)
```text
        assert!(ca_store.normalized, error::invalid_state(E_NORMALIZATION_REQUIRED));
        assert!(ca_store.transfers_received > 0, error::invalid_state(E_NOTHING_TO_ROLLOVER));
```

**File:** aptos-move/framework/aptos-framework/sources/confidential_asset/confidential_asset.move (L784-786)
```text
        ca_store.normalized = false;
        ca_store.transfers_received = 0;
        ca_store.pending_balance = new_zero_pending_compressed();
```
