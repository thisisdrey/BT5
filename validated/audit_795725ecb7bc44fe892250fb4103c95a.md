[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L31-35)
```text
  function registerAddress(Roles role, address addr) external {
    // forge-lint: disable-next-line(incorrect-shift)
    addressBook[msg.sender][addr] |= (1 << uint8(role));
    emit AddressRegistered(msg.sender, role, addr);
  }
```

**File:** tare-io__tare-contracts/contracts/misc/LoansAuth.sol (L69-87)
```text
  function approveOriginator(address user) public onlyRole(GUARDIAN_ROLE) {
    addressBook[address(this)][user] |= ORIGINATOR_MASK;
    emit AddressRegistered(address(this), Roles.Originator, user);
    emit OriginatorApproved(user);
  }

  /// @inheritdoc ILoansAuth
  function revokeOriginator(address user) public onlyAdminOrGuardian {
    addressBook[address(this)][user] &= ~ORIGINATOR_MASK;
    emit AddressUnregistered(address(this), Roles.Originator, user);
    emit OriginatorRevoked(user);
  }

  /// @inheritdoc ILoansAuth
  function approveServicer(address user) public onlyRole(GUARDIAN_ROLE) {
    addressBook[address(this)][user] |= SERVICER_MASK;
    emit AddressRegistered(address(this), Roles.Servicer, user);
    emit ServicerApproved(user);
  }
```

**File:** tare-io__tare-contracts/specs/loan_permissions.md (L224-238)
```markdown
### Validation Rules

1. **Loan Creation**: All addresses (borrower, investor, servicer) must be registered in the **originator's** address book with appropriate roles before calling `create()`
2. **Borrower Updates**: When updating a borrower via `updateBorrower()`, the new borrower address must be registered in the **servicer's** address book
3. **Address Book Ownership**: Each entity manages their own address book independently

```solidity
// Loan creation validates against originator's address book
require(isRegisteredForRole(originator, Roles.Borrower, borrower), UnregisteredAddress(borrower));
require(isRegisteredForRole(originator, Roles.Investor, investor), UnregisteredAddress(investor));
require(isRegisteredForRole(originator, Roles.Servicer, servicer), UnregisteredAddress(servicer));

// Borrower updates validate against servicer's address book
require(isRegisteredForRole(servicers[loanId], Roles.Borrower, newBorrower), UnregisteredAddress(newBorrower));
```
```

**File:** tare-io__tare-contracts/specs/loan_permissions.md (L248-260)
```markdown
### Canonical Address Book

The contract itself (`address(this)`) serves as the canonical address book for protocol-level approvals:

- Approved originators are registered in the canonical book with `Roles.Originator`
- Approved servicers are registered in the canonical book with `Roles.Servicer`

```solidity
function approveOriginator(address user) external onlyRole(GUARDIAN_ROLE);
function revokeOriginator(address user) external; // requires admin or guardian
function approveServicer(address user) external onlyRole(GUARDIAN_ROLE);
function revokeServicer(address user) external; // requires admin or guardian
```
```

**File:** tare-io__tare-contracts/test/misc/LoansAuth.t.sol (L177-199)
```text
  function test_AddressBook_IsolatedBetweenOwners() public {
    address sharedAddr = makeAddr("sharedAddr");
    address originatorB = makeAddr("originatorB");

    // Register sharedAddr in originator's book only
    vm.prank(originator);
    auth.registerAddress(Roles.Borrower, sharedAddr);

    // Should be registered for originator but NOT for originatorB
    assertTrue(auth.isRegisteredForRole(originator, Roles.Borrower, sharedAddr));
    assertFalse(auth.isRegisteredForRole(originatorB, Roles.Borrower, sharedAddr));

    // Now register in originatorB's book
    vm.prank(originatorB);
    auth.registerAddress(Roles.Investor, sharedAddr);

    // Each book should only have their own registration for a different role
    assertTrue(auth.isRegisteredForRole(originator, Roles.Borrower, sharedAddr));
    assertFalse(auth.isRegisteredForRole(originator, Roles.Investor, sharedAddr));

    assertFalse(auth.isRegisteredForRole(originatorB, Roles.Borrower, sharedAddr));
    assertTrue(auth.isRegisteredForRole(originatorB, Roles.Investor, sharedAddr));
  }
```
