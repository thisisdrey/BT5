### Title
Attacker-controlled `contract_id` in `StorageDeposit` intent permanently destroys signer's wNEAR with no refund on failure - (File: contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs)

### Summary
`Contract::storage_deposit` in `contracts/defuse/src/contract/intents/state.rs:265-297` debits the signer's internal wNEAR balance by `storage_deposit.amount`, unwraps it to native NEAR, and then fires `do_storage_deposit` which forwards the NEAR as an attached deposit to `ext_storage_management::storage_deposit` on an attacker-supplied `contract_id`. `do_storage_deposit` in `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs:14-26` never attaches a resolve/callback that checks whether that final cross-contract call succeeded, so if the target reverts, there is no path that re-credits the signer.

### Finding Description
The binding the protocol must maintain is:
`internal_sub_balance(signer_id, wnear) == amount actually deposited as storage stake for deposit_for_account_id on contract_id, OR refunded back to signer_id`.

Trace:
1. `Contract::storage_deposit` (`contracts/defuse/src/contract/intents/state.rs:265-297`) calls `self.withdraw(owner_id, [(Nep141TokenId(wnear_id), amount)], Some("withdraw"), false)`, which internally calls `internal_sub_balance` and irreversibly debits the signer's ledger balance of wNEAR.
2. It then calls `ext_wnear::near_withdraw(amount)` followed by `.then(Self::do_storage_deposit(storage_deposit))`.
3. `do_storage_deposit` (`contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs:14-26`) only checks that the preceding `near_withdraw` promise succeeded (`promise_result_checked_void(0)`), then issues `ext_storage_management::ext(storage_deposit.contract_id).with_attached_deposit(storage_deposit.amount)...storage_deposit(...)` and returns that `Promise` directly with no further `.then()` resolving its outcome.
4. Since `contract_id` is attacker-supplied and unvalidated, the attacker deploys a contract whose `storage_deposit` function always panics. When the final promise fails, the NEAR runtime refunds the attached deposit to the **predecessor of that failed receipt**, which is the Defuse contract account itself (the direct caller), not the signer, and there is no callback in `do_storage_deposit` to detect the failure and credit the signer's internal ledger back.
5. Consequently the signer's internal wNEAR balance was already debited in step 1 and is never restored, while `deposit_for_account_id` receives zero storage stake on `contract_id`, and any refunded NEAR lands as raw NEAR balance on the Defuse contract account, not attributed to anyone's ledger balance.

This differs from the pattern used elsewhere (e.g. FT/NFT/MT withdraw paths which use `notify_and_resolve_transfer`/resolve callbacks to re-credit failed transfers) — `do_storage_deposit` has no equivalent resolve step at all.

### Impact Explanation
The signer's wNEAR balance inside the Verifier ledger is permanently destroyed while no corresponding value is created anywhere (not on `contract_id`, not back in the signer's balance). This is a batch whose balance changes do not net to zero: the Verifier's on-chain wNEAR liability was reduced by `amount` for the signer with nothing settled in return, which is a Critical-severity "user funds permanently frozen/destroyed" outcome per the stated criteria. It's fully repeatable by any account holding wNEAR, against any of their own attacker-deployed malicious FT-storage-management contracts, for any amount up to their balance.

### Likelihood Explanation
Low complexity, no special privileges. The attacker only needs: a signer account with wNEAR balance, and a self-deployed contract implementing (or pretending to implement) `storage_management` whose `storage_deposit` fn always panics. They sign a `StorageDeposit` intent naming their own contract as `contract_id`, submit via `execute_intents`, and the described loss occurs deterministically on first execution. Cost is only the destroyed amount itself and gas, so this is primarily a self-harm-shaped bug, but it demonstrates a genuine missing-refund defect: the same lack of resolve/refund logic would also apply if a *third party's* honest storage-management contract happened to reject a deposit (e.g., insufficient deposit for registration, or account limits), causing accidental fund loss for users who aren't attackers at all — the deliberate "attacker controls contract_id" framing in the question is just the cleanest way to force the failure reliably.

### Recommendation
Add a resolve callback to `do_storage_deposit` (private `#[private]` method) that inspects the result of `ext_storage_management::storage_deposit`, and on failure, re-credits the signer's internal ledger balance with `storage_deposit.amount` in wNEAR token units, mirroring the resolve/refund pattern already used for FT/NFT/MT withdrawals (`notify_and_resolve_transfer`).

### Proof of Concept
```
cargo test proof plan (near-workspaces sandbox):
1. Deploy Defuse contract + wNEAR contract + a malicious FT/storage contract `MockRevertingStorage`
   whose `storage_deposit` panics unconditionally.
2. Fund `signer` with wNEAR inside the Defuse ledger (deposit + internal_add_balance).
3. Record `balance_before = Contract::mt_balance_of(signer, wnear_token_id)`.
4. Sign a `StorageDeposit { contract_id: mock_reverting_storage, deposit_for_account_id: signer, amount }`
   intent as `signer`, submit via `execute_intents`.
5. Await promise resolution.
6. Assert `Contract::mt_balance_of(signer, wnear_token_id) == balance_before - amount` (debited).
7. Assert `mock_reverting_storage.storage_balance_of(signer) == None / 0` (nothing deposited).
8. Assert no compensating credit exists anywhere for `signer` — confirming the debited amount 
   is unaccounted for and permanently lost.
```