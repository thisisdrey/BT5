### Title
Unprivileged dust-deposit griefing saturates `MAX_TOKENS_PER_USER`, blocking a victim from adding collateral or debt and forcing liquidation - (File: contracts/Pool.sol)

### Summary
`Pool` tracks every deposit/debt token a user holds in `depositTokensOfAccount` / `debtTokensOfAccount` (`MappedEnumerableSet`) and enforces a shared cap via `onlyIfAdditionWillNotReachMaxTokens`. Entries are added not by the user but as a side effect of any `DepositToken._mint`/`_transfer` whose recipient previously held zero balance - and the recipient's consent is never required. An unprivileged attacker can therefore call `DepositToken.deposit(dust, victim)` (or `transfer(victim, dust)`) once per listed collateral, saturating the victim's slot count so that every subsequent `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` call reverts with `UserReachedMaxTokens`. This mirrors CVE-2024-26529's class: a remote unauthenticated request drives a state handler into a condition where further legitimate operations fail (DoS).

### Finding Description
- `Pool.addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` revert once `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (`Pool.sol:143-148`, `:204-220`). [1](#0-0) [2](#0-1) 
- `DepositToken._mint` unconditionally calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance is zero, and `deposit(amount_, onBehalfOf_)` mints to an arbitrary `onBehalfOf_` (`DepositToken.sol:211-237`, `:485-488`). [3](#0-2) [4](#0-3) 
- `_transfer` does the same for the recipient (`DepositToken.sol:517-525`). [5](#0-4) 
- `DebtToken` mint/issue likewise calls `pool.addToDebtTokensOfAccount` when the borrower's balance goes from 0, so new debt positions also revert once saturated.
- Removal only happens when the victim's balance of that exact token returns to 0 (`DepositToken.sol:459-462`, `:522-525`; `Pool.removeFromDepositTokensOfAccount`, `Pool.sol:630-634`). [6](#0-5) 

Attack path: attacker calls `depositToken_i.deposit(1 wei, victim)` for each of the pool's listed deposit tokens (depositing on behalf of the victim, so the attacker only needs dust of each underlying). After ~`MAX_TOKENS_PER_USER` slots are filled, any of the following for the victim revert:
- depositing a collateral type they don't already hold (`_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`),
- issuing a new debt token type (`addToDebtTokensOfAccount` → revert),
- receiving any deposit-token transfer of a token they don't already hold.

The attacker can cheaply front-run any cleanup by re-dusting a freed slot, since the victim must empty a dust token's balance to 0 to free a slot.

### Impact Explanation
Temporary freezing of funds / forced liquidation. A victim approaching liquidation cannot deposit a *new* collateral type to restore health — the `deposit` reverts inside `_mint` before funds can help — so their position can be liquidated while a remediation transaction is griefed. Inbound transfers/mints of unfamiliar tokens to the victim revert entirely. Recovery requires the victim to transfer each dust token to a fresh address (locked-balance checks via `_revertIfLocked`/`unlockedBalanceOf` can additionally prevent moving dust when the position is unhealthy, since unlocked balance shrinks as health degrades), and each freed slot can be re-griefed in the same block at dust cost.

### Likelihood Explanation
- Fully unprivileged: only requires dust amounts of each listed underlying and public `deposit(amount_, onBehalfOf_)` / `transfer` calls, or the same via `Operator.execute` batching (`Operator.sol:34-55`). [7](#0-6) 
- No modifier stops it: `deposit` is `whenNotPaused nonReentrant onlyIfDepositTokenExists`; there is no opt-in or allowlist on `onBehalfOf_`, and `SynthContext._msgSender` resolution doesn't change the beneficiary path.
- Cost scales with the number of listed deposit tokens (capped by the pool's own `MAX_TOKENS_PER_USER` deposit-token cap); on pools with several collaterals the attack is affordable. It is most damaging when timed against positions nearing the liquidation threshold.

### Recommendation
- Require recipient opt-in before an account is enrolled in `depositTokensOfAccount`, or only add tokens on actions initiated by the account itself (e.g., add the entry in `deposit` only when `onBehalfOf_ == _msgSender()`, and add a `claim`/whitelist step for third-party transfers).
- Alternatively, don't revert on saturated `add` — make `addToXTokensOfAccount` best-effort and move per-account accounting off the critical path, or decouple the token list from deposit correctness (iterate over the global token list for health checks).
- Allow users to force-remove a token entry for an account (`removeFromXTokensOfAccount` callable for any account when balance is 0) so griefed dust can be purged without transferring tokens.

### Proof of Concept
Hardhat fork sketch:

```ts
// fork a chain where Pool has N listed deposit tokens
const victim = bob.address;
for (const dt of depositTokens) {           // each listed DepositToken
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  await getDust(underlying, attacker);      // acquire 1 wei of underlying
  await underlying.connect(attacker).approve(dt.address, 1);
  // attacker deposits on behalf of victim — victim never consented
  await dt.connect(attacker).deposit(1, victim);
}
// now: getDepositTokensOfAccount(victim).length + getDebtTokensOfAccount(victim).length >= MAX_TOKENS_PER_USER

// 1) victim cannot deposit a collateral type it doesn't already hold
await underlyingNew.connect(victim).approve(newDt.address, amount);
await expect(newDt.connect(victim).deposit(amount, victim))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) victim cannot open a new debt position
await expect(newDebtToken.connect(victim).issue(1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) third-party transfer of a new deposit token to victim reverts
await expect(dt2.connect(alice).transfer(victim, 1)).to.be.reverted;

// 4) while saturated, price move makes victim unhealthy; victim cannot add new
//    collateral -> liquidator seizes position via pool.liquidate(...)
```

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

**File:** contracts/DepositToken.sol (L211-234)
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
```

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
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

**File:** contracts/Operator.sol (L34-55)
```text
    function execute(
        Call[] calldata calls_
    ) external payable override nonReentrant setMsgSender returns (bytes[] memory _returnData) {
        uint256 _length = calls_.length;
        _returnData = new bytes[](_length);

        uint256 _sumOfValues;
        Call calldata _call;
        for (uint256 i; i < _length; ) {
            _call = calls_[i];
            uint256 _value = _call.value;
            unchecked {
                _sumOfValues += _value;
            }
            _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
            unchecked {
                ++i;
            }
        }

        require(msg.value == _sumOfValues, "value-mismatch");
    }
```
