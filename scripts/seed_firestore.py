#!/usr/bin/env python3
"""Seed script for populating Firestore with initial pantry inventory data."""

from google.cloud import firestore

PROJECT_ID = "qwiklabs-gcp-03-2ef87ee9c932"

INITIAL_PANTRY_ITEMS = [
    {
        "id": "olive_oil",
        "name": "Extra Virgin Olive Oil",
        "quantity": 1.5,
        "unit": "liters",
        "category": "Oils & Condiments",
        "expiration_date": "2026-12-31",
        "notes": "Cold pressed, organic",
    },
    {
        "id": "organic_eggs",
        "name": "Organic Large Eggs",
        "quantity": 12,
        "unit": "eggs",
        "category": "Dairy & Eggs",
        "expiration_date": "2026-10-15",
        "notes": "Grade A pasture-raised",
    },
    {
        "id": "arborio_rice",
        "name": "Arborio Rice",
        "quantity": 2.0,
        "unit": "kg",
        "category": "Grains & Pasta",
        "expiration_date": "2027-05-30",
        "notes": "Ideal for risotto",
    },
    {
        "id": "fresh_garlic",
        "name": "Fresh Garlic Bulbs",
        "quantity": 4,
        "unit": "bulbs",
        "category": "Produce",
        "expiration_date": "2026-10-25",
        "notes": "Store in cool dark place",
    },
    {
        "id": "roma_tomatoes",
        "name": "Roma Tomatoes",
        "quantity": 8,
        "unit": "items",
        "category": "Produce",
        "expiration_date": "2026-10-05",
        "notes": "Ripe and ready for sauce",
    },
]


def seed_database():
    print(f"Connecting to Firestore (Project ID: {PROJECT_ID})...")
    db = firestore.Client(project=PROJECT_ID)
    collection_ref = db.collection("pantry_items")

    for item in INITIAL_PANTRY_ITEMS:
        doc_id = item["id"]
        doc_ref = collection_ref.document(doc_id)
        doc_ref.set(item)
        print(f"  - Seeded item: {item['name']} (ID: {doc_id})")

    print("✅ Firestore seeding completed successfully!")


if __name__ == "__main__":
    seed_database()
