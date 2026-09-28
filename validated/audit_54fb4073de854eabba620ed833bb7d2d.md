### Title
Unprivileged attacker can permanently block a victim from receiving new deposit/debt tokens by filling `depositTokensOfAccount` to `MAX_TOKENS_PER_USER` via dust transfers - ([contracts/Pool.sol](contracts/Pool.sol), [contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
Analogous to CVE-2021-22906 (an authenticated user can lock resources of other users), any EOA can "lock" a victim's position slot in `Pool` by dust-filling the per-account token lists. `Pool` enforces `MAX_TOKENS_PER_USER = 30` across the combined `debtTokensOfAccount + depositTokensOfAccount` length, reverting with `UserReachedMaxTokens` when full [1](#0-0) [2](#0-1) . Entries are added permissionlessly: any first-time recipient of a `DepositToken` — via `deposit(amount_, onBehalfOf_)`, `transfer`, or `transferFrom` — is appended to `depositTokensOfAccount` by `_mint`/`_transfer` [3](#0-2) [4](#0-3) . There is no minimum deposit amount (`amount_ == 0` check only) [5](#0-4) , no opt-in, and no victim consent.

### Finding Description
- `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens(account_)`, which reverts when the victim's combined list length reaches 30 [6](#0-5) .
- On deployments with many listed collaterals/synthetics (e.g., base/optimism deployments), an attacker deposits 1 wei of every whitelisted collateral `onBehalfOf_ = victim`, and/or transfers 1 wei of each `msdTOKEN` to the victim, filling the list to 30.
- After that, any transaction that would append a new token to the victim's account reverts inside the mint/issue path:
  - `DepositToken.deposit(x, victim)` for any not-yet-listed collateral — the victim cannot onboard new collateral types (including via `NativeTokenGateway`/`VesperGateway`).
  - `DebtToken.issue(...)` minting a debt token the victim doesn't already hold — the victim cannot open new debt positions, so leverage/flash-mint paths in `SmartFarmingManager` also revert.
  - Plain `msdTOKEN.transfer(victim, ...)` for tokens the victim doesn't hold — even receiving seized collateral as a liquidator (`seize` → `_transfer`) can revert for the beneficiary.
- Front-running: the attacker can top-up to 30 in the same block as the victim's deposit/issue transaction, sustaining the DoS indefinitely at dust cost.

### Impact Explanation
Temporary/conditional freezing of victim funds and protocol functionality: while the list is saturated, the victim cannot deposit new collateral types to rescue a deteriorating position (contributing to liquidation of an otherwise saveable position — indirect loss of collateral), cannot issue any new debt token, and cannot receive new `msdTOKEN`s. The victim can recover by zeroing out dust entries (transfer/withdraw), but the attacker can re-fill at near-zero cost per new listing, making the freeze effectively persistent as long as ≥30 distinct deposit tokens exist and the attacker is willing to front-run.

### Likelihood Explanation
Medium: requires ≥30 supported deposit+debt tokens combined and an attacker willing to deposit negligible dust across them; both are realistic on mature deployments. No privileged role needed — `deposit(onBehalfOf_)` and `transfer` are public, use `_msgSender()`/`SynthContext` only for the sender side, and no pause flag stops `transfer`.

### Recommendation
- Reject deposits/transfers that would add a token to `onBehalfOf_`/`recipient`'s list when it hits `MAX_TOKENS_PER_USER` only if the recipient is the sender; better: enforce a meaningful `minDeposit`/first-deposit threshold, or
- Allow removal paths to always succeed and/or count entries only when balance is economically meaningful, or
- Gate third-party additions: only credit `depositTokensOfAccount` on `deposit` when `onBehalfOf_ == _msgSender()` or when the recipient has opted in.

### Proof of Concept
Hardhat sketch against deployed Pool/DepositTokens:

```ts
// Fill victim's token list with 30 dust entries
for (const dt of depositTokens.slice(0, 30)) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, victim.address) // adds dt to victim's list
}

// Victim is now saturated; any new-token credit reverts
await expect(
  newDepositToken.connect(attacker).deposit(parseEther('1'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

await expect(
  debtToken.connect(victim).issue(parseEther('1'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

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

**File:** contracts/DepositToken.sol (L215-216)
```text
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();
```

**File:** contracts/DepositToken.sol (L486-488)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L518-520)
```text
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
