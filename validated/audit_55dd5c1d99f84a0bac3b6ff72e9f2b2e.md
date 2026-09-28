### Title
Unprivileged attacker can permanently fill a victim's `depositTokensOfAccount` list with dust transfers, DoS-ing deposits and issuance - (File: contracts/Pool.sol)

### Summary
`Pool` caps the number of distinct debt/deposit tokens an account may hold at `MAX_TOKENS_PER_USER = 30` and reverts with `UserReachedMaxTokens` whenever a *new* token would be added to an account's list [1](#0-0) . `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` on a recipient's first receipt, with no opt-in and no minimum amount [2](#0-1) . Because `transfer`/`transferFrom` only validate the *sender's* unlocked balance [3](#0-2) , any EOA can push 1 wei of every registered `DepositToken` into a victim's account, consuming all 30 slots and making every subsequent `deposit()` of a *new* collateral type — and every first-time `issue()` of a new `DebtToken` — revert for that account. This is the Metronome analog of the CVE's bug class: an unprivileged local user degrading availability of a subsystem (per-position collateral/issuance).

### Finding Description
Relevant code paths:

- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` enforce `onlyIfAdditionWillNotReachMaxTokens` before inserting [4](#0-3) .
- `DepositToken._transfer` adds the token to the recipient's per-account set whenever `_recipientBalanceBefore == 0 && amount_ > 0` [5](#0-4) .
- `DepositToken.transfer` only calls `_revertIfLocked` on the sender; the recipient is not consulted [3](#0-2) .
- `DepositToken._mint` triggers the same `addToDepositTokensOfAccount` on a first-time deposit, so a revert there propagates out of `deposit()` [6](#0-5) .

Attack steps (all public entry points, unprivileged EOA):

1. For each registered `DepositToken` in the victim's pool, attacker calls `deposit(dust, attacker)` (dust = smallest unit; deposit only requires the token be active and under `maxTotalSupply`).
2. Attacker calls `transfer(victim, 1)` on each `DepositToken`. Each call inserts that token into `depositTokensOfAccount[victim]` since victim's prior balance was 0.
3. After ≤30 transfers, `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER`, and `onlyIfAdditionWillNotReachMaxTokens(victim)` reverts permanently.

Cost is bounded: 1 wei per token at deposit + transfer gas; the attacker can withdraw the remainder afterwards.

### Impact Explanation
- The victim cannot `deposit()` any collateral type they don't already hold — every mint path hits `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
- The victim cannot open a debt position in a `DebtToken` they haven't used (`addToDebtTokensOfAccount` reverts) and cannot receive any `DepositToken` transfer or `seize` proceeds into a new token.
- A victim near liquidation who needs to add a *different* collateral to restore health is forced into liquidation — direct loss of funds via denial of the rescue path.
- Funds already deposited remain withdrawable (`withdraw`/`_withdraw` do not add tokens), so existing collateral is not permanently locked; the impact is denial of new deposits/issuance and forced-liquidation exposure, i.e., availability degradation consistent with the accepted "temporary freezing of funds / protocol availability" class.

### Likelihood Explanation
- Requires only an EOA and dust amounts of each underlying; no privileged role, no oracle manipulation, no flash loan needed (though one could fund the dust deposits).
- The only limiting factors: the pool must have multiple registered `DepositToken`s (deployment configs list several per pool), and the victim must not already occupy the slots.
- Recovery exists: the victim can `transfer` the 1-wei dust out, which removes each entry (`balanceOf[sender_] == 0` path), but each entry only clears when its balance hits exactly zero, and griefed dust can be re-sent at any time (re-griefing is cheap and can be front-run around any transaction that needs a new token, e.g., a rescue deposit or a `liquidate`/`seize` into a new token).

### Recommendation
- Add a minimum first-deposit/transfer threshold, or track membership only above a dust cutoff, so 1-wei receipts don't occupy a slot.
- Alternatively, let recipients purge entries via a public `removeFromDepositTokensOfAccount`-style escape when balance is below a threshold, or exempt `seize`/liquidation inflows from the cap so liquidations and emergency collateralization cannot be griefed.

### Proof of Concept
Hardhat/fork sketch (assumes a deployed `Pool` with ≥1 `DebtToken` and multiple `DepositToken`s, e.g. from `deployments/mainnet`):

```ts
// attacker: EOA; victim: any account with < 30 token entries
const pool = await ethers.getContractAt('Pool', POOL)
const depositTokens: string[] = await pool.getDepositTokens() // registered collaterals

for (const addr of depositTokens) {
  const dt = await ethers.getContractAt('DepositToken', addr)
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  // fund attacker with dust, then:
  await underlying.connect(attacker).approve(dt.address, dust)
  await dt.connect(attacker).deposit(dust, attacker.address)   // get msdTOKEN balance
  await dt.connect(attacker).transfer(victim.address, 1)       // occupies victim slot
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(depositTokens.length)

// victim now tries to deposit a collateral type they never held:
const newDt = await ethers.getContractAt('DepositToken', someTokenNotYetHeld)
await expect(
  newDt.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// likewise, issuing a new debt type reverts via addToDebtTokensOfAccount
await expect(
  someNewDebtToken.connect(victim).issue(1, victim.address) // via Pool issue flow
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Caveats: if a pool's combined registered deposit + debt tokens are fewer than `MAX_TOKENS_PER_USER`, the attacker fills only what's available, which still blocks all *new* token types unless the victim clears slots; exact `minDepositTime`/lock behavior on dust-sized deposits was not fully verified in this pass and should be confirmed when writing the fork test.

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

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
