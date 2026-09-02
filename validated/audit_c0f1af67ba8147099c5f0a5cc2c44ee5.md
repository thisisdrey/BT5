### Title
Malicious `ft_transfer_call` receiver return value causes double-credit in `ft_resolve_withdraw` - ([File: contracts/defuse/src/contract/tokens/nep141/withdraw.rs])

### Summary
`ft_resolve_withdraw` computes the refund solely from the JSON `U128` returned by the external `ft_transfer_call` promise, without any check against the contract's actual custody of the token. Because deposits/withdrawals are permissionless for arbitrary NEP-141 `AccountId`s, an attacker who deploys and controls the `token` contract can make the real transfer succeed in full while lying in the resolve response, causing `internal_ft_withdraw`'s single debit of `amount` to be effectively reversed by a phantom `deposit` refund of the same amount.

### Finding Description
Binding: `debited_from_ledger(1000) == delivered_to_attacker_via_transfer(1000) + refunded_back_to_ledger(1000)` should read `1000 == 1000 + 0`; instead the exploit makes it `1000 == 1000 + 1000`.

Path: `ft_withdraw` (`contracts/defuse/src/contract/tokens/nep141/withdraw.rs:27-50`) → `internal_ft_withdraw` debits `token_balances[owner_id][token]` by `amount` via `Contract::withdraw` (`contracts/defuse/src/contract/tokens/mod.rs:76-128`), then schedules `do_ft_withdraw` (`withdraw.rs:117-151`), which — because `msg` is `Some(...)` — issues a real `ft_transfer_call` to `withdraw.token`. The callback `ft_resolve_withdraw` (`withdraw.rs:154-195`) computes:
```
used = promise_result_checked_json::<U128>(0) -> Ok(Ok(used)) => used.0.min(amount.0)
refund = amount.0.saturating_sub(used)
self.deposit(sender_id, [(token, refund)], REFUND_MEMO)
```
This trusts the JSON body returned by the promise call to `withdraw.token`'s `ft_transfer_call`, which per NEP-141 is produced entirely by that same external contract. Since deposits/withdrawals accept any `AccountId` as `token` with no allowlist (`ft_on_transfer` in `contracts/defuse/src/contract/tokens/nep141/deposit.rs:19-43` accepts any `predecessor_account_id()`), an attacker can deploy a `token` contract whose `ft_transfer_call` implementation actually delivers the full `amount` to `receiver_id` (satisfying the on-chain balance change) but returns `U128(0)` in its promise result. `ft_resolve_withdraw` then treats the entire `amount` as unused and re-credits `sender_id`'s `token_balances` for that `TokenId` via `Contract::deposit`, which increments `total_supplies` and `owner.token_balances` (`contracts/defuse/src/contract/tokens/mod.rs:18-74`) with no cross-check against real custody of the underlying token.

No existing guard intercepts this: there is no allowlist restricting which NEP-141 contracts can be used as `token`, and `ft_resolve_withdraw`'s only sanity clamp is `used.0.min(amount.0)`, which does not defend against an artificially low `used` value.

### Impact Explanation
For the specific `TokenId` corresponding to the attacker's own malicious FT contract, the internal ledger (`token_balances` and `total_supplies`) is inflated by the withdrawn amount with no backing custody — a resolver credit ("refund") that does not correspond to anything that actually failed to settle, matching the Critical category "a refund or resolver credit that does not match what failed to settle." The attacker can repeat this per withdrawal call against their own self-issued token, inflating an internally-recognized balance (visible via `mt_balance_of`/NEP-245 views) that has zero real backing. This is fully repeatable and costs the attacker only the gas and the trivial one-time deployment of a malicious FT contract.

### Likelihood Explanation
Preconditions are minimal and fully attacker-controlled: deploy a compliant-looking NEP-141 contract, deposit units of it into Defuse via `ft_on_transfer` (permissionless, any token accepted), then call `ft_withdraw` with `msg: Some(...)` to route through the `ft_transfer_call` path, and have the malicious contract deliver tokens but report `used = U128(0)`. No privileged role, relayer key, or victim signature is required — the attacker only manipulates a token type they themselves created and control.

### Recommendation
Do not trust the arbitrary JSON body returned by an external `ft_transfer_call` for tokens that are not verified/known-good. Cross-check the resolver's `used`/refund logic against the contract's own recorded state of the transfer (e.g., only refund based on confirmed promise failure/rejection, not a success value under the callee's control), or restrict withdrawable `token` contracts to an allowlist/known-safe implementation set, or require `ft_transfer` (non-call) semantics for withdrawal accounting so refund is based on `PromiseResult` success/failure rather than an attacker-supplied numeric payload.

### Proof of Concept
`cargo test` plan (near-workspaces/sandbox, non-mainnet):
1. Deploy a mock FT contract `malicious_ft` whose `ft_transfer_call` implementation performs a real internal balance transfer of the full `amount` to `receiver_id` but returns `PromiseOrValue::Value(U128(0))` in its resolved JSON.
2. As `attacker`, deposit `1000` of `malicious_ft` into the Defuse contract (`ft_on_transfer`), asserting `token_balances[attacker][malicious_ft] == 1000` pre-call.
3. Call `ft_withdraw(token=malicious_ft, receiver_id=attacker, amount=U128(1000), msg=Some("x"))`.
4. Assert LHS `debited = 1000` (from `Contract::withdraw`) against RHS `delivered(1000) + refunded`; check `malicious_ft` balance of `attacker` equals `1000` (real tokens delivered) AND `token_balances[attacker][malicious_ft]` in Defuse equals `1000` post-call (refunded), proving `1000 == 1000 + 1000` rather than `1000 == 1000 + 0`.