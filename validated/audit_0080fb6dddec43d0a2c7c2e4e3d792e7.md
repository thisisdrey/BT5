### Title
Attacker can permanently fill a victim's per-account token list with dust deposits, DoS-ing deposits of new collateral and new debt issuance - (File: contracts/Pool.sol)

### Summary

`Pool` enforces `MAX_TOKENS_PER_USER = 30` across `depositTokensOfAccount + debtTokensOfAccount`. Anyone can add entries to an arbitrary victim's list by calling `DepositToken.deposit(1 wei, victim)` once per registered deposit token (or by transferring dust `msdTOKEN`s). Once the list reaches 30 entries, every action that would add a new token to the account — `deposit` of a new collateral, `issue` of a new synthetic, `transfer`/`transferFrom` of a deposit token to the victim, and liquidation `seize` payouts routed to the account — reverts with `UserReachedMaxTokens`, freezing the victim's ability to top-up collateral or issue debt.

### Finding Description

The cap is enforced in `onlyIfAdditionWillNotReachMaxTokens`: [1](#0-0) 

```solidity
if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
    revert UserReachedMaxTokens();
}
```

`addToDepositTokensOfAccount` is invoked unconditionally whenever a recipient's `DepositToken` balance goes from 0 to >0 — both on `deposit(amount_, onBehalfOf_)` (mint path) and on ERC20 `transfer`/`transferFrom`: [2](#0-1) [3](#0-2) 

`deposit` takes an arbitrary `onBehalfOf_` beneficiary with no opt-in, so an unprivileged attacker can deposit 1 wei (or the smallest unit) of every registered `DepositToken` directly to a victim. Each call appends one entry to `depositTokensOfAccount[victim]`. The same vector exists via `DepositToken.transfer(victim, 1)` for tokens the attacker already holds.

Symmetric logic exists on the debt side: `addToDebtTokensOfAccount` is called from `DebtToken` issue/mint when an account's balance moves off zero. [4](#0-3) 

Removal only happens when a balance returns to exactly zero (`removeFromDepositTokensOfAccount`), so a victim whose msdTOKENs are locked as collateral for open debt (`_revertIfLocked` in `unlockedBalanceOf`) cannot clear the dust entries — the dust sits in `balanceOf` but cannot be transferred out, making the DoS persist until the victim fully repays, which itself may be impossible without depositing new collateral.

### Impact Explanation

Availability / temporary-to-indefinite freezing of funds flows, matching the DoS bug class:

- Victim cannot `deposit` any collateral type they don't already hold — the underlying is pulled to `Treasury` first, but the tx reverts in `_mint` → `addToDepositTokensOfAccount`, so the deposit is denied.
- Victim cannot `issue` a synthetic token they haven't borrowed before (`addToDebtTokensOfAccount` reverts), so a position approaching liquidation cannot mint-and-sell or rebalance into a different synth.
- Nobody (including liquidators routing seized collateral via `seize` → `_transfer` to `to_`, or anyone sending msdTOKENs) can transfer a new deposit token type to the victim.
- If the victim's existing msdTOKEN balances are fully locked (`unlockedBalanceOf == 0`), the entries cannot be removed by the victim, so the block lasts as long as the debt position remains — potentially forcing an otherwise-avoidable liquidation (loss of collateral via liquidation fees/bad debt resolution).

No privileged role is required; the attacker only needs public `deposit`/`transfer` and dust amounts. `whenNotPaused`, `nonReentrant`, `SynthContext` and supply-cap checks do not prevent it.

### Likelihood Explanation

Requires the pool to have enough registered deposit/debt token types for the attacker (plus victim's existing entries) to reach 30 combined slots. Cost per slot is a single dust deposit/transfer, so the attack is cheap where the token count is sufficient. The victim has no way to opt out or preempt the list additions, and cleanup is impossible while collateral is locked by debt. Not all deployments may list enough tokens to fully saturate the cap for a fresh account, but partially-filled accounts and pools with many collaterals are exposed.

### Recommendation

- Make the per-account token list opt-in or lazy: only add deposit tokens on `deposit`/`issue`, not on plain `transfer`, or let recipients receive without list registration.
- Allow accounts to remove arbitrary entries from their own `depositTokensOfAccount`/`debtTokensOfAccount` lists (a `removeTokenFromMyList(token)` escape hatch) so dust-griefing is self-cleanable even when balances are locked.
- Alternatively, revert `addTo*` failures softly: skip the cap check revert on unsolicited receipt paths (`transfer`/`seize`), or exclude zero-value/dust positions from the cap by tracking a minimum meaningful balance.

### Proof of Concept

Hardhat sketch against mainnet `Pool`/`DepositToken` deployments (e.g. `deployments/mainnet/Pool.json`):

```ts
// setup: attacker holds or acquires 1 wei of collateral for each of N deposit tokens
const victim = victimAddress;
const depositTokens = await pool.getDepositTokens(); // registered collaterals

for (const dt of depositTokens) {
  const depositToken = await ethers.getContractAt('DepositToken', dt);
  const underlying = await ethers.getContractAt('IERC20', await depositToken.underlying());
  await underlying.connect(attacker).approve(depositToken.address, 1);
  // fills depositTokensOfAccount[victim] — no victim consent needed
  await depositToken.connect(attacker).deposit(1, victim);
}

// once debtTokensOfAccount[victim] + depositTokensOfAccount[victim] == 30:
// 1) any deposit of a new collateral type to victim reverts
await expect(
  newDepositToken.connect(victim).deposit(amount, victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) issuing a new synthetic (debt token not yet in list) reverts
await expect(
  newDebtToken.connect(victim).issue(amount)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) if victim's msdTOKENs are locked by debt, victim cannot transfer dust
//    out (NotEnoughFreeBalance) => entries cannot be removed => persistent DoS
```

Note: confirm the target pool has enough registered token types (plus the victim's existing entries) to reach `MAX_TOKENS_PER_USER = 30`; the attacker's per-slot cost is one dust `deposit`/`transfer` call.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-220)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L485-489)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
