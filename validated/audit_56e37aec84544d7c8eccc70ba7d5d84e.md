## Analysis

The reported bug class — crediting/accounting based on the *declared* transfer amount instead of the amount actually received, which breaks on fee-on-transfer tokens like USDT — maps directly onto the NEP-141 deposit path of the Defuse contract.

### The vulnerable binding

`Contract::deposit` credits internal MT balances based on the `amount` argument it is given, with no verification that this amount was actually received by the contract: [1](#0-0) 

This is called from `ft_on_transfer`, the NEP-141 receiver hook, using the `amount` parameter supplied by the *token contract* in its `ft_transfer_call` callback — not a value independently verified by Defuse: [2](#0-1) 

Per the NEP-141 standard, `amount` in `ft_on_transfer` is the amount the token contract's ledger recorded as being sent to the predecessor (Defuse), which is only guaranteed to equal the true balance increase for well-behaved, non-fee tokens. For a fee-on-transfer token (e.g. USDT-style, or any custom NEP-141 with a transfer fee), the actual tokens received by Defuse can be less than `amount`, yet `deposit()` unconditionally credits the depositor for the full `amount.0`, and there is no whitelist or fee-awareness mechanism restricting which NEP-141 contracts can call `ft_on_transfer`.

### Consequence — broken conservation invariant

The invariant that should hold is: `sum(mt-credited balances for token T) == actual token T balance held by the Defuse contract`. Because deposits are credited on the "value debited" side (what the token contract claims it moved) rather than "value delivered", an attacker can:

1. Send a fee-on-transfer NEP-141 token via `ft_transfer_call` with `amount = X`, while the Defuse contract's balance only increases by `X - fee`.
2. `ft_on_transfer` credits the attacker `X` internal units for that `TokenId`, exceeding the actual tokens held.
3. Withdraw via `ft_withdraw`/`internal_ft_withdraw`, debiting the internal ledger and issuing an outbound `ft_transfer`/`ft_transfer_call` for the requested amount: [3](#0-2) 

If the attacker (or, more damagingly, later legitimate depositors of the same token) later withdraw against this phantom over-credited pool, real liquidity contributed by other honest depositors of the same token can be drained to cover the phantom balance — an unauthorized transfer of value between users sharing the same `TokenId` pool, and/or permanent insolvency/freezing of legitimate depositors' withdrawable funds once the phantom balance is redeemed first.

Given the escrow-swap contract (`contracts/escrow-swap/**`) is explicitly out of scope, and this deposit/credit logic lives in `contracts/defuse/src/contract/tokens/**`, which is in scope, this is a valid analog.

### Title
Fee-on-transfer NEP-141 tokens cause phantom over-crediting on deposit, breaking the credited-balance-to-held-balance invariant - (File: `contracts/defuse/src/contract/tokens/mod.rs`)

### Summary
`Contract::deposit`, invoked from `ft_on_transfer` in `contracts/defuse/src/contract/tokens/nep141/deposit.rs`, credits a depositor's internal multi-token balance using the `amount` value reported by the depositing NEP-141 token contract, without verifying that this amount was actually received. For any NEP-141 token that deducts a fee on transfer, the Defuse contract's real token balance increases by less than `amount`, yet the internal ledger is credited the full `amount`.

### Finding Description
`ft_on_transfer` ( [2](#0-1) ) passes the `amount: U128` argument it is given by `predecessor_account_id()` (the token contract) directly to `self.deposit(...)`. `deposit()` ( [1](#0-0) ) increments `total_supplies` and the owner's `token_balances` by that exact `amount`, with no comparison against an actual balance delta observed on the token contract. Any NEP-141 contract can call `ft_on_transfer` as its `predecessor_account_id`, and there is no allow-list restricting deposits to fee-free tokens. For a fee-on-transfer token, the amount credited internally therefore exceeds the amount of tokens the Defuse contract custody actually holds for that `TokenId`, breaking the equality `sum(credited balances for token T) == tokens held for T`.

### Impact Explanation
This is a Critical-class issue per the custody-binding categories: value is credited without the corresponding value being delivered (analogous to "value debited versus value delivered plus refunded"). Once phantom balance is credited, subsequent withdrawals by the same or other holders of that `TokenId` (`internal_ft_withdraw` / `withdraw`, [3](#0-2)  ) can drain real token liquidity that was deposited by other, honest holders of the same token, resulting in some depositors being permanently unable to withdraw their legitimately deposited balance (funds frozen) — or in effect an unauthorised transfer of value from later depositors to the attacker who initially triggered the fee-on-transfer deposit.

### Likelihood Explanation
Any unprivileged account holding, or able to acquire, a fee-on-transfer NEP-141 token (such tokens exist and are commonly bridged/wrapped on NEAR-compatible ecosystems) can trigger this simply by calling `ft_transfer_call` to the Defuse contract — no special role, relayer, or privileged account is required, and no additional conditions beyond the token having a transfer fee.

### Recommendation
In `ft_on_transfer` (and the analogous `mt_on_transfer`/`nft_on_transfer` hooks if applicable), measure the actual balance delta of the token held by the contract (e.g. by comparing `ft_balance_of(current_account_id)` before and after, or requiring `amount` to be validated against a post-transfer balance check) and credit the depositor with the actually-received amount rather than the caller-declared `amount`. Alternatively, restrict deposits to a vetted allow-list of tokens known not to charge transfer fees.

### Proof of Concept
1. Deploy or use an NEP-141 token contract that deducts, e.g., a 10% fee on `ft_transfer`/`ft_transfer_call` (fee-on-transfer semantics), predecessor account being the token contract in `ft_on_transfer`.
2. Attacker calls `ft_transfer_call(defuse_contract, amount=1000, msg="")`. The token contract's internal accounting reduces attacker's balance by 1000 but only transfers 900 to Defuse's underlying balance (retaining 100 as fee) — matching real-world USDT-fee-style behavior.
3. `ft_on_transfer` is invoked with `amount = U128(1000)`; `Contract::deposit` credits attacker 1000 units of that `TokenId` ( [4](#0-3) ), while Defuse only actually holds 900 tokens.
4. A second honest user deposits 500 of the same token (fee reduces real receipt to 450, but they're credited 500 as well — same bug, compounding the shortfall).
5. Attacker calls `ft_withdraw` for 1000 units. `internal_ft_withdraw` debits the attacker's 1000-unit balance and issues `ft_transfer` for 1000 real tokens ( [5](#0-4) ), which succeeds because the pool now holds 900 + 450 = 1350 real tokens — draining tokens that belong to the second depositor.
6. When the honest second depositor later attempts to withdraw their credited 500 units, the pool no longer holds enough real tokens, and their withdrawal fails/reverts (funds frozen), demonstrating the loss transferred from the honest depositor to the attacker.

### Citations

**File:** contracts/defuse/src/contract/tokens/mod.rs (L38-64)
```rust
        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            mint_event.token_ids.to_mut().push(token_id.to_string());
            mint_event.amounts.to_mut().push(amount);

            let total_supply = self
                .storage
                .state
                .total_supplies
                .add(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
            match token_id {
                TokenId::Nep171(ref tid) => {
                    if total_supply > 1 {
                        return Err(DefuseError::NftAlreadyDeposited(tid.clone()));
                    }
                }
                TokenId::Nep141(_) | TokenId::Nep245(_) | TokenId::Imt(_) => {}
            }

            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
```

**File:** contracts/defuse/src/contract/tokens/nep141/deposit.rs (L19-43)
```rust
    fn ft_on_transfer(
        &mut self,
        sender_id: AccountId,
        amount: U128,
        msg: String,
    ) -> PromiseOrValue<U128> {
        require!(amount.0 > 0, "zero amount");

        let token_id = TokenId::Nep141(Nep141TokenId::new(env::predecessor_account_id()));

        let DepositMessage {
            receiver_id,
            action,
        } = if msg.is_empty() {
            DepositMessage::new(sender_id.clone())
        } else {
            msg.parse().unwrap_or_else(|e| panic!("{e}"))
        };

        self.deposit(
            receiver_id.clone(),
            [(token_id.clone(), amount.0)],
            Some("deposit"),
        )
        .unwrap_or_else(|err| err.panic());
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L53-74)
```rust
impl Contract {
    pub(crate) fn internal_ft_withdraw(
        &mut self,
        owner_id: AccountId,
        withdraw: FtWithdraw,
        force: bool,
    ) -> Result<PromiseOrValue<U128>> {
        self.withdraw(
            &owner_id,
            iter::once((
                Nep141TokenId::new(withdraw.token.clone()).into(),
                withdraw.amount,
            ))
            .chain(withdraw.storage_deposit.map(|amount| {
                (
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    amount.as_yoctonear(),
                )
            })),
            Some("withdraw"),
            force,
        )?;
```
