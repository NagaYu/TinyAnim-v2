#!/usr/bin/env python3
"""
Create the TinyAnim "Pro" product + recurring Price in your Stripe account
(test mode by default) and print the Price id to paste into STRIPE_PRICE_ID.

Usage:
    STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_bootstrap.py
    STRIPE_SECRET_KEY=sk_test_... python scripts/stripe_bootstrap.py --amount 900 --currency usd
"""

from __future__ import annotations

import argparse
import os
import sys

import stripe


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap TinyAnim Stripe product/price")
    parser.add_argument("--amount", type=int, default=900, help="price in the smallest currency unit (default 900 = $9.00)")
    parser.add_argument("--currency", default="usd")
    parser.add_argument("--interval", default="month", choices=["month", "year"])
    args = parser.parse_args()

    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        print("ERROR: set STRIPE_SECRET_KEY (use a sk_test_... key for test mode).", file=sys.stderr)
        return 1
    stripe.api_key = key

    product = stripe.Product.create(name="TinyAnim Pro", description="Unlimited optimizations, 50MB files, API access.")
    price = stripe.Price.create(
        product=product["id"],
        unit_amount=args.amount,
        currency=args.currency,
        recurring={"interval": args.interval},
    )

    print("\n✅ Created Stripe product & price (mode:", "test" if key.startswith("sk_test_") else "LIVE", ")")
    print("   Product:", product["id"])
    print("   Price  :", price["id"])
    print("\nPaste this into your environment:")
    print(f"   STRIPE_PRICE_ID={price['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
