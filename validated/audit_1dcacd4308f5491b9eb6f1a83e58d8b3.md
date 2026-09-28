### Title
Dust-transfer griefing fills `depositTokensOfAccount`, blocking all new deposits and borrows for a victim — (File: contracts/Pool.sol)

### Summary
`Pool` tracks every `DepositToken`/`DebtToken` an account holds in `MappedEnumerableSet.AddressSet` lists capped at `MAX_TOKENS_PER_USER = 30` [1](#0-0) . `DepositToken._transfer` adds the token to the recipient's list on any nonzero receipt, with no opt-in [2](#0-1) . An unprivileged attacker can therefore dust-transfer all supported deposit tokens to a victim, permanently occupying their 30 slots and causing `UserReachedMaxTokens` reverts on every new deposit-type or debt-type the victim tries to use.

### Finding Description
`addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` enforce `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` → revert [3](#0-2) . Entries are pushed into `depositTokensOfAccount` both on mint (deposit) and on plain ERC20 `transfer`/`transferFrom` receipt [4](#0-3) . `transfer` only requires the *sender's* unlocked balance — there is no check on the recipient [5](#0-4) .

Attack path (unprivileged EOA):
1. For each of the pool's deposit tokens (up to `MAX_TOKENS_PER_USER`, also the protocol cap on registered deposit tokens [6](#0-5) ), attacker deposits a minimal amount via `DepositToken.deposit(1, attacker)`.
2. Attacker calls `depositToken.transfer(victim, 1)`. Each transfer adds that deposit token to `victim`'s list [2](#0-1) .
3. Once 30 slots are filled, every subsequent `deposit` of a *new* collateral type reverts in `_mint → pool.addToDepositTokensOfAccount` with `UserReachedMaxTokens` [7](#0-6) , and any `DebtToken.issue` of a synthetic the victim hasn't held reverts in `addToDebtTokensOfAccount` [8](#0-7) .

If the victim already holds debt, dusting tokens they don't own (without sending their held ones) still fills remaining slots and blocks them from depositing *new* collateral types to restore health before liquidation — while `liquidate` remains callable on them. Recovery requires the victim to notice and manually `transfer` dust out of each token to free slots, which the attacker can front-run/re-fill at negligible cost since deposits of 1 wei are permitted (only `AmountIsZero` is rejected [9](#0-8) ). No modifier stops this: `whenNotPaused`, `nonReentrant`, `onlyIfDepositTokenExists` are all satisfied on the deployed configuration, and `_revertIfLocked` only constrains the attacker.

### Impact Explanation
Invariant broken: liveness — a user cannot open new collateral/debt token positions. Concretely: (a) a debt-free victim is denied protocol entry until they clean 30 dust entries; (b) a victim with an unhealthy position who only owns slots already filled can be prevented from adding a *different* collateral to avoid liquidation, causing loss of collateral to liquidators. This matches the "temporary freezing of funds / blocked operations by an unprivileged attacker" impact class. Cost to the attacker is bounded (30 dust deposits + 30 transfers, all reclaimable), and it is repeatable per victim.

### Likelihood Explanation
Requires only standard public entry points (`deposit`, `transfer`) and dust amounts; no privileged role, oracle manipulation, or external precondition. Mitigating factor: the victim can self-recover slots by transferring dust out, and an already-diversified position can still use its existing token types — so the worst case (liquidation while blocked) needs the victim to lack a usable existing collateral type.

### Recommendation
Do not mutate the recipient's `depositTokensOfAccount` list on plain `transfer`/`transferFrom`; only register tokens on `deposit`/`_mint` (or add an opt-in flag). Alternatively, derive per-account collateral enumeration on demand, or make `addToDepositTokensOfAccount` not count tokens whose balance is below a dust threshold. At minimum, allow `removeFromDepositTokensOfAccount` to be triggered by the victim through a dedicated `cleanUp` path in `Pool`.

### Proof of Concept
Reproducible on a Hardhat fork of a deployment with ≥1 registered deposit token:

```ts
// victim starts with empty lists
expect(await pool.getDepositTokensOfAccount(victim.address)).to.be.empty

// attacker deposits dust in up to 30 deposit tokens and transfers 1 wei to victim
for (const dt of depositTokens) {
  await underlying.connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, attacker.address)
  await dt.connect(attacker).transfer(victim.address, 1)
}

// victim's list is now full of attacker-chosen tokens
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30)

// victim cannot deposit a collateral type not already in their list
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// victim cannot mint a synthetic whose DebtToken isn't already in their list
await expect(
  newDebtToken.connect(victim).issue(amount, victim.address) // via pool path
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Note: whether this specific grief vector is a previously reported/known issue should be checked against prior audit reports; the mechanics above are confirmed present in the current code.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-147)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
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

**File:** contracts/Pool.sol (L703-705)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
```

**File:** contracts/DepositToken.sol (L215-215)
```text
        if (amount_ == 0) revert AmountIsZero();
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
