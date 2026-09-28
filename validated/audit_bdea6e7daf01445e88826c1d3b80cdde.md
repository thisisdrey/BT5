### Title
Dust-transfer griefing fills a victim's `MAX_TOKENS_PER_USER` slots, DoS-ing new deposits and debt issuance - (File: contracts/DepositToken.sol)

### Summary
`Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once an account holds 30 distinct deposit/debt tokens (`Pool.sol:143-148`). Any holder of a `DepositToken` can permissionlessly add that token to an arbitrary victim's per-account list by `transfer()`-ing dust to them, because `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to non-zero (`DepositToken.sol:518-520`). An unprivileged attacker can therefore fill all 30 slots of any victim, after which every code path that would register a *new* token for the victim — `DepositToken.deposit(..., onBehalfOf_=victim)` → `_mint` → `addToDepositTokensOfAccount` (`DepositToken.sol:486-488`) and `DebtToken.issue`/`mint` → `_mint` → `addToDebtTokensOfAccount` (`DebtToken.sol:597-600`) — permanently reverts. This is a direct analog of the reported availability bug class: an attacker-triggered, repeatable denial of service of core protocol functionality for a targeted user.

### Finding Description
- `Pool.MAX_TOKENS_PER_USER = 30` caps `debtTokensOfAccount + depositTokensOfAccount` length; the modifier `onlyIfAdditionWillNotReachMaxTokens` reverts `UserReachedMaxTokens` when the combined list is full (`Pool.sol:79`, `Pool.sol:143-148`).
- `DepositToken._transfer` adds the token to the *recipient's* list on first receipt (`DepositToken.sol:518-520`); the public `transfer`/`transferFrom` entry points only check the *sender's* unlocked balance (`DepositToken.sol:348-376`), so anyone holding deposit tokens can push dust to a victim.
- Once the victim's list is saturated, the same account-side check fires inside `_mint`/`_transfer`/`DebtToken._mint`, making every subsequent first-time token registration for that victim revert.
- Escape is not guaranteed: clearing a slot requires zeroing a balance, but `transfer`/`withdraw` are gated by `_revertIfLocked` → `unlockedBalanceOf` (`DepositToken.sol:180-182, 383-398`). If the victim carries any debt, the dust amounts received are locked and cannot be moved out, so the victim cannot free slots until fully repaying debt — and repaying debt itself may be impossible if it requires issuing a *new* synthetic to repay, or adding a *new* collateral type to stay healthy.
- No modifier stops this: `transfer` is not behind `whenNotPaused`, the recipient has no opt-in/opt-out, and `SynthContext` sender resolution is irrelevant since the attacker calls the token directly.

### Impact Explanation
Temporary-to-permanent freezing of funds and denial of position management:
- The victim cannot deposit any collateral type they don't already hold (`deposit` reverts at `_mint` → `addToDepositTokensOfAccount`), so a victim approaching liquidation cannot diversify into a new collateral to restore health and is pushed toward liquidation/bad debt.
- The victim cannot mint a synthetic they have no existing debt position in (`issue`/`SmartFarmingManager.leverage` → `DebtToken._mint` reverts), blocking all new borrowing.
- While under-collateralized, the attacker-donated tokens are locked (`unlockedBalanceOf` returns 0 when `issuableInUsd` is 0), so the victim cannot even shed the dust slots to recover — the freeze persists until their debt is gone.
- Cost to attacker: dust transfers of existing pool deposit tokens only; fully unprivileged, no privileged role, oracle manipulation, or malicious endpoint required.

### Likelihood Explanation
High. The attack needs only public `transfer` calls with attacker-owned msd balances. Number of effective slots equals the number of distinct deposit/debt tokens registered in the pool; on deployments with several collaterals the attacker may need to combine forced transfers with the victim's existing holdings, but any single first-receipt of any pool token counts. No governance action, flash capital, or price manipulation is needed. The invariant broken is liveness of per-account token registration, which is enforced exactly by the code that reverts.

### Recommendation
- Do not register a token into `depositTokensOfAccount`/`debtTokensOfAccount` on plain `transfer` to a recipient that never deposited; only register on `deposit`/`issue`/`seize`, or make registration a no-op (lazy) rather than reverting.
- Alternatively, allow recipients to opt out (whitelisting) or allow any account to remove a token from its own list regardless of locked balance (e.g., a `discardToken(address)` that burns the dust to the treasury), so victims can always free slots.
- Raise or remove the cap if the enumeration is only needed off-chain / for health checks that can be bounded differently.

### Proof of Concept
```solidity
// Hardhat/Foundry-style, pool with >= 30 registered deposit tokens dT0..dT29
// Attacker holds >=1 wei of each (obtainable by depositing 1 wei of each underlying).
for (uint i = 0; i < 30; i++) {
    depositToken[i].transfer(victim, 1); // adds token i to victim's list via _transfer -> addToDepositTokensOfAccount
}
// victim's depositTokensOfAccount.length == 30

// Any new-token registration for victim now reverts:
vm.expectRevert(UserReachedMaxTokens.selector);
newDepositToken.deposit(100e18, victim);          // _mint -> pool.addToDepositTokensOfAccount -> revert

vm.expectRevert(UserReachedMaxTokens.selector);
newDebtToken.issue(1e18, victim);                  // _mint -> pool.addToDebtTokensOfAccount -> revert

// Victim cannot clear slots while carrying debt:
vm.expectRevert(NotEnoughFreeBalance.selector);
depositToken[0].connect(victim).transfer(attacker, 1); // dust locked: unlockedBalanceOf(victim) == 0
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** contracts/DepositToken.sol (L180-182)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
    }
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L498-526)
```text
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
    }
```

**File:** contracts/DebtToken.sol (L572-601)
```text
    function _mint(
        IPool pool_,
        IMasterOracle masterOracle_,
        address account_,
        uint256 amount_
    ) private onlyIfDebtTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToNullAddress();

        uint256 _debtFloorInUsd = pool_.debtFloorInUsd();
        uint256 _balanceBefore = balanceOf(account_);

        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }

        totalSupply_ += amount_;
        if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();

        principalOf[account_] = _balanceBefore + amount_;
        debtIndexOf[account_] = debtIndex;
        emit Transfer(address(0), account_, amount_);

        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
    }
```
