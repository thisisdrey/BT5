Looking at the codebase, I need to find an analog to the BondingTax bug class: a valid parameter value of zero causes a downstream function to revert, leading to DoS on core swap functionality.

Let me trace the price path carefully.