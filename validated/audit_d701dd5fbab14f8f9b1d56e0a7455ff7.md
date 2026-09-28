### Title
Unprivileged dust deposits fill a victim's `MAX_TOKENS_PER_USER` slots, blocking new deposits and mints - (File: contracts/Pool.sol)

### Summary
The JavaFX advisory (BIT-java-2026-47013) is an unauthenticated, network-reachable partial DoS. The Metronome analog is the `MAX_TOKENS_PER_USER = 30` cap enforced by `Pool.onlyIfAdditionWillNotReachMaxTokens`. Because any registered `DepositToken` can call `Pool.addToDepositTokensOfAccount` on behalf of an arbitrary `account_`, and `DepositToken.deposit(amount_, onBehalfOf_)` mints to an attacker-chosen beneficiary, an EOA can force a victim's combined `debtTokensOfAccount + depositTokensOfAccount` list to the 30-token cap with dust amounts. Once at cap, the victim cannot deposit into any collateral type they don't already hold and cannot issue debt in any synthetic they don't already owe - both paths revert with `UserReachedMaxTokens`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
- `Pool.addToDepositTokensOfAccount` is callable only by registered deposit tokens, but it adds to `account_`'s list unconditionally whenever the balance transitions `0 -> >0` inside `DepositToken._transfer` / `DepositToken._mint`. [4](#0-3) 
- `DepositToken.deposit` is public, `whenNotPaused`, and takes `onBehalfOf_`; a 1-wei underlying deposit mints dust msdTOKEN to the victim and inserts that deposit token into their list. [2](#0-1) 
- The cap is enforced on both insertion paths (`onlyIfAdditionWillNotReachMaxTokens` wraps `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount`), so once `debt + deposit` tokens held = 30, every subsequent `_mint`/`_transfer` that would create a new slot reverts. [1](#0-0) [5](#0-4) 
- Debt tokens are non-transferable (`TransferNotSupported`), so the debt side can't be pushed by the attacker; the attack surface is exactly the pool's registered deposit tokens. The attack succeeds if `registeredDepositTokens >= 30 - victimDebtTokenCount`. The mainnet deployment carries ~a dozen deposit tokens historically (msdWETH, msdWBTC, Vesper wrappers, etc.); a victim already holding several debt/deposit tokens can be pushed over the cap with the remaining slots. [6](#0-5) 
- No modifier stops it: `deposit` has no cap check on the beneficiary before minting, `SynthContext._msgSender` is the attacker not the victim, and `updateRewardsBeforeMintOrBurn` does not block the insertion. A targeted use is griefing a near-liquidation victim: while saturated, they cannot deposit a *new* collateral type to repair health, though they can still add to already-held collateral.

### Impact Explanation
Temporary freezing of funds / denial of core protocol functions for a targeted user. The victim cannot open new collateral slots or new debt slots until they manually clear dust positions (transfer/withdraw the 1-wei balances to trigger `removeFromDepositTokensOfAccount`). Combined with a deteriorating position, this can prevent adding fresh collateral types while a liquidation is pending, enabling otherwise-blocked liquidations. Recovery is possible but costs the victim transactions and time.

### Likelihood Explanation
Reachable by any EOA with dust of each underlying (or via `transfer` of existing msdTOKEN dust). Cost is `O(#depositTokens)` transfers plus trivial token amounts. The precondition - that the pool registers enough deposit tokens to fill the cap given the victim's existing positions - depends on deployed configuration; with fewer registered tokens than the cap, the attack only saturates available slots (still blocking all *new* collateral types). Recovery by the victim reduces severity.

### Recommendation
Track a per-account "opt-in" flag or require `addToDepositTokensOfAccount` to only add when the *sender's* balance first becomes nonzero on deposits (i.e., in `_mint`, call `addToDepositTokensOfAccount` only for `account_ == _msgSender()` or make deposit-list membership driven by the beneficiary's explicit action). Alternatively, exempt `_transfer`-based additions from triggering the cap revert and instead skip adding unsolicited dust tokens whose transferred amount is below a minimum deposit threshold.

### Proof of Concept
```solidity
// Hardhat/Foundry fork sketch (mainnet fork, real Pool + DepositTokens)
// Assume: victim has D debt/deposit tokens; pool has >= 30 - D registered deposit tokens.
uint256 slots = 30 - pool.debtTokensOfAccount(victim).length() - pool.depositTokensOfAccount(victim).length();
address[] memory dts = pool.getDepositTokens(); // registered deposit tokens
for (uint256 i; i < slots; ++i) {
    IDepositToken dt = IDepositToken(dts[i]);
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 1);
    underlying.approve(address(dt), 1);
    dt.deposit(1, victim); // mints dust msdTOKEN to victim, adds slot
}
// victim deposit into a new collateral now reverts:
vm.expectRevert(UserReachedMaxTokens.selector);
otherDepositToken.deposit(amount, victim);
// victim minting a new synthetic debt also reverts via addToDebtTokensOfAccount
```

Uncertainty: I could not fully confirm the deployed count of registered deposit tokens on each chain's current Pool configuration from the index alone; the attack's completeness depends on that count versus the victim's existing slot usage.

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

**File:** contracts/DebtToken.sol (L507-509)
```text
    function transfer(address /*recipient_*/, uint256 /*amount_*/) external override returns (bool) {
        revert TransferNotSupported();
    }
```

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
