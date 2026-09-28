### Title
Dust-filling of the 30-entry per-account token list permanently blocks victims from adding new collateral or debt positions (`Pool.addToDepositTokensOfAccount` / `Pool.addToDebtTokensOfAccount`) - (File: contracts/Pool.sol)

### Summary
CVE-2022-21426 is a JAXP resource-exhaustion DoS: an unauthenticated caller forces a service into repeated work/reverts. The Metronome analog is the per-account token-list cap: `Pool.MAX_TOKENS_PER_USER = 30`, enforced by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once a user's combined `debtTokensOfAccount + depositTokensOfAccount` list has 30 entries. An unprivileged attacker can fill a victim's list with dust deposit-token positions — via `DepositToken.deposit(amount, onBehalfOf_ = victim)` or plain `DepositToken.transfer(victim, dust)` — because `_mint`/`_transfer` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` when the recipient's prior balance was 0. Once the list is full, every new deposit-token deposit/mint and every `DebtToken.issue` to the victim reverts.

### Finding Description
`Pool` tracks each account's collateral and debt positions in two `MappedEnumerableSet` lists to bound the `debtOf`/`depositOf` oracle loops, capped at 30 combined entries: [1](#0-0) [2](#0-1) 

`DepositToken._mint` and `DepositToken._transfer` push a new entry for any first-time holder, and `deposit()` accepts an arbitrary `onBehalfOf_` beneficiary: [3](#0-2) [4](#0-3) [5](#0-4) 

The sender-side lock check (`_revertIfLocked`) only constrains the attacker's own unlocked balance; the victim's consent is never required. Entries are only removed when a balance returns to zero (`removeFromDepositTokensOfAccount`), which the victim can do for dust positions by withdrawing/transferring them — but while the list is full the victim cannot:

- deposit any collateral type they don't already hold (`deposit` → `_mint` → `addToDepositTokensOfAccount` reverts `UserReachedMaxTokens`),
- mint any synthetic they don't already owe (`DebtToken.issue` → mint path → `addToDebtTokensOfAccount` reverts),
- receive liquidated collateral of a new type (liquidator is unaffected; the victim only loses, never receives),
- use `SmartFarmingManager.leverage`/`flashRepay` paths that mint new tokens to the account.

Notably the revert occurs *inside* the mint/transfer, so even legitimate counterparties (e.g., someone repaying the victim's seized collateral back, or a zap depositing on their behalf) cannot credit the victim a new token type.

### Impact Explanation
DoS analog of the JAXP finding: an unauthenticated remote attacker degrades a victim's available functionality. Concretely, an account nearing liquidation that needs to add a *different* collateral type to stay healthy is blocked from doing so, enabling forced liquidation and loss of funds. The freeze is temporary/conditional (the victim can burn dust positions to free slots, paying withdraw fees and gas on up to 30 unwanted positions, and can still interact with token types already in their list), matching the "partial denial of service" and "temporary freezing of funds" categories.

### Likelihood Explanation
Fully unprivileged: any EOA can call `deposit(amount, victim)` on each of the pool's deposit tokens with a dust amount, or `transfer(victim, dust)` for tokens the attacker holds. Cost is bounded by `maxTotalSupply` (must exceed 0), gas for ~30 calls, and dust collateral. No privileged role, oracle manipulation, or governance action is needed; `MAX_TOKENS_PER_USER` is a hard constant and the deployed pools (mainnet/base/swell/hemi) ship this implementation.

### Recommendation
Make list removal symmetric and voluntary: e.g., allow `deposit`/`_transfer` to succeed but skip `addToDepositTokensOfAccount` when the cap is reached (tracking the position only in the unbounded balance mapping and crediting it in `depositOf` separately), or add a per-account opt-out / sweep function, or charge a minimum first-deposit threshold. Alternatively, let the receiver remove any deposit-token entry on demand via a public `removeFromDepositTokensOfAccount`-style rescue path gated by `account_ == msg.sender`.

### Proof of Concept
Hardhat fork sketch (mainnet pool):

```ts
const pool = await ethers.getContractAt('Pool', POOL); // mainnet Pool proxy
const depositTokens = await pool.getDepositTokens();    // all listed collateral
const victim = victimAddr;

// victim starts with an existing position (or empty list)
for (const dt of depositTokens) {
  const depositToken = await ethers.getContractAt('DepositToken', dt);
  const underlying = await ethers.getContractAt('IERC20', await depositToken.underlying());
  // attacker acquires dust of each underlying (AMM swap), approves, deposits on behalf of victim
  await underlying.approve(dt, DUST);
  await depositToken.deposit(DUST, victim); // adds token to victim's list
}
// fill remaining slots with dust transfers of deposit tokens attacker already holds

// victim's list is now at MAX_TOKENS_PER_USER (30)
expect((await pool.getDepositTokensOfAccount(victim)).length
  + (await pool.getDebtTokensOfAccount(victim)).length).to.eq(30);

// 1) victim cannot deposit a new collateral type
const newDt = await ethers.getContractAt('DepositToken', NEW_DEPOSIT_TOKEN);
await expect(newDt.connect(victimSigner).deposit(amount, victim))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 2) victim cannot issue a new synthetic (adds debt token)
const debtToken = await ethers.getContractAt('DebtToken', NEW_DEBT_TOKEN);
await expect(debtToken.connect(victimSigner).issue(amount, victim))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Fork preconditions: at least one deposit token with `maxTotalSupply > 0` and `isActive`, pool not paused/shutdown. Each `deposit(DUST, victim)` succeeds because `quoteDepositOut` on a dust amount still mints `> 0` (choose DUST above the fee rounding floor), triggering `addToDepositTokensOfAccount(victim)` until the cap is hit; every subsequent new-token mint to the victim reverts.

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

**File:** contracts/DepositToken.sol (L486-488)
```text
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
