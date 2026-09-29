# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import datetime
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional
from zoneinfo import ZoneInfo

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from dotenv import load_dotenv
from google import genai
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.cloud import firestore, storage
from google.genai import types

from app.a2ui_utils import a2ui_callback

# Load environment variables from local .env
load_dotenv()

# Hardcoded GCP Project ID & Cloud Storage Bucket Name
PROJECT_ID = "qwiklabs-gcp-03-2ef87ee9c932"
BUCKET_NAME = "smart-pantry-media-qwiklabs-gcp-03-2ef87ee9c932"

# Initialize Firestore Client
db = firestore.Client(project=PROJECT_ID)

# Load Agent Engine resource name from deployment_metadata.json if present
agent_engine_resource_name = None
metadata_file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deployment_metadata.json")
if not os.path.exists(metadata_file_path):
    metadata_file_path = "deployment_metadata.json"

if os.path.exists(metadata_file_path):
    try:
        with open(metadata_file_path, "r") as f:
            metadata = json.load(f)
            agent_engine_resource_name = metadata.get("remote_agent_runtime_id")
    except Exception as e:
        print(f"Warning: Could not read deployment_metadata.json: {e}")

code_executor = AgentEngineSandboxCodeExecutor(
    agent_engine_resource_name=agent_engine_resource_name
)


# WRITE: after each turn, send the session to Memory Bank for extraction.
async def generate_memories_callback(callback_context: CallbackContext):
    await callback_context.add_session_to_memory()
    return None


def list_pantry_items(category: Optional[str] = None) -> str:
    """Lists items currently in the pantry inventory.

    Args:
        category: Optional category string (e.g. 'Produce', 'Dairy & Eggs', 'Grains & Pasta', 'Oils & Condiments') to filter by.

    Returns:
        A string listing matching pantry items with quantity, unit, and expiration details.
    """
    try:
        collection_ref = db.collection("pantry_items")
        if category:
            query = collection_ref.where("category", "==", category)
            docs = query.stream()
        else:
            docs = collection_ref.stream()

        items = []
        for doc in docs:
            data = doc.to_dict()
            items.append(
                f"- {data.get('name')} (ID: {doc.id}): {data.get('quantity')} {data.get('unit')} "
                f"[{data.get('category')}] | Expires: {data.get('expiration_date', 'N/A')} | Notes: {data.get('notes', '')}"
            )

        if not items:
            return "No pantry items found."

        return "Current Pantry Inventory:\n" + "\n".join(items)
    except Exception as e:
        return f"Error reading pantry items from Firestore: {e}"


def add_or_update_pantry_item(
    item_id: str,
    name: str,
    quantity: float,
    unit: str,
    category: str,
    notes: str = "",
    expiration_date: str = "",
) -> str:
    """Adds a new item or updates an existing item in the pantry inventory.

    Args:
        item_id: Unique identifier string for the item (e.g., 'olive_oil', 'fresh_basil').
        name: Display name of the ingredient/item.
        quantity: Current quantity number (e.g. 2.0).
        unit: Unit of measurement (e.g. 'liters', 'items', 'kg', 'bulbs').
        category: Category string (e.g. 'Produce', 'Dairy & Eggs', 'Grains & Pasta', 'Oils & Condiments').
        notes: Optional extra description or storage notes.
        expiration_date: Optional expiration date string (YYYY-MM-DD).

    Returns:
        A confirmation message of the addition or update.
    """
    try:
        item_data = {
            "id": item_id.lower().replace(" ", "_"),
            "name": name,
            "quantity": float(quantity),
            "unit": unit,
            "category": category,
            "notes": notes,
            "expiration_date": expiration_date,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        doc_ref = db.collection("pantry_items").document(item_id.lower().replace(" ", "_"))
        doc_ref.set(item_data, merge=True)
        return f"Successfully updated '{name}' in pantry (Quantity: {quantity} {unit})."
    except Exception as e:
        return f"Error writing pantry item to Firestore: {e}"


def find_expiring_items(days_threshold: int = 30) -> str:
    """Finds pantry items that are already expired or expiring within the next N days.

    Args:
        days_threshold: Number of days ahead to check for upcoming expiration (default is 30 days).

    Returns:
        A string listing expiring/expired items with their expiration dates and quantities.
    """
    try:
        today = datetime.date.today()
        cutoff_date = today + datetime.timedelta(days=days_threshold)

        collection_ref = db.collection("pantry_items")
        docs = collection_ref.stream()

        expiring_items = []
        for doc in docs:
            data = doc.to_dict()
            exp_str = data.get("expiration_date", "")
            if exp_str:
                try:
                    exp_date = datetime.date.fromisoformat(exp_str)
                    if exp_date <= cutoff_date:
                        status = "EXPIRED" if exp_date < today else "EXPIRING SOON"
                        expiring_items.append(
                            f"- [{status}] {data.get('name')}: {data.get('quantity')} {data.get('unit')} (Expires: {exp_str})"
                        )
                except ValueError:
                    pass

        if not expiring_items:
            return f"No items are expired or expiring within the next {days_threshold} days."

        return f"Items expiring within {days_threshold} days (as of {today.isoformat()}):\n" + "\n".join(expiring_items)
    except Exception as e:
        return f"Error checking expiring items: {e}"


def search_external_recipes(query: str) -> str:
    """Searches for real online recipes by dish name or main ingredient via TheMealDB API.

    Args:
        query: Recipe title or main ingredient to search for (e.g. 'pasta', 'chicken', 'egg').

    Returns:
        A list of matching recipe titles, categories, instructions, and video/thumbnail links.
    """
    try:
        api_key = os.getenv("THEMEALDB_API_KEY", "1")
        encoded_query = urllib.parse.quote(query.strip())
        url = f"https://www.themealdb.com/api/json/v1/{api_key}/search.php?s={encoded_query}"

        req = urllib.request.Request(url, headers={"User-Agent": "SmartPantryAgent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())

        meals = data.get("meals")
        if not meals:
            filter_url = f"https://www.themealdb.com/api/json/v1/{api_key}/filter.php?i={encoded_query}"
            req_filter = urllib.request.Request(filter_url, headers={"User-Agent": "SmartPantryAgent/1.0"})
            with urllib.request.urlopen(req_filter, timeout=5) as resp_filter:
                filter_data = json.loads(resp_filter.read().decode())
            meals = filter_data.get("meals")

        if not meals:
            return f"No external recipes found matching '{query}'."

        results = []
        for meal in meals[:3]:
            title = meal.get("strMeal")
            category = meal.get("strCategory", meal.get("strArea", "Recipe"))
            instructions = meal.get("strInstructions", "")
            if instructions and len(instructions) > 200:
                instructions = instructions[:197] + "..."
            thumbnail = meal.get("strMealThumb", "")

            summary = f"- **{title}** [{category}]"
            if instructions:
                summary += f"\n  Instructions: {instructions}"
            if thumbnail:
                summary += f"\n  Image: {thumbnail}"
            results.append(summary)

        return f"Found external recipes for '{query}':\n\n" + "\n\n".join(results)
    except Exception as e:
        return f"Error searching external recipes API: {e}"


def geocode_address(address: str) -> str:
    """Converts a street address or city name into geographic coordinates (latitude and longitude) using Google Geocoding API.

    Args:
        address: The location, address, or city to geocode (e.g., '1600 Amphitheatre Parkway, Mountain View, CA').

    Returns:
        A formatted string with the latitude, longitude, and official formatted address.
    """
    try:
        api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        if not api_key:
            return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

        encoded_address = urllib.parse.quote(address.strip())
        url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded_address}&key={api_key}"

        req = urllib.request.Request(url, headers={"User-Agent": "SmartPantryAgent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())

        if data.get("status") != "OK" or not data.get("results"):
            return f"Could not geocode address '{address}': {data.get('error_message', data.get('status'))}"

        result = data["results"][0]
        formatted_address = result.get("formatted_address")
        location = result["geometry"]["location"]
        lat = location["lat"]
        lng = location["lng"]

        return f"Geocoded Location:\n- Address: {formatted_address}\n- Latitude: {lat}\n- Longitude: {lng}"
    except Exception as e:
        return f"Error calling Geocoding API: {e}"


def find_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "supermarket",
    radius_meters: float = 2000.0,
) -> str:
    """Finds nearby places (e.g. grocery stores, supermarkets, bakeries) of a given type using Google Places API (New).

    Args:
        latitude: Latitude of the center location.
        longitude: Longitude of the center location.
        place_type: Type of place to search for (e.g., 'supermarket', 'grocery_store', 'bakery'). Default is 'supermarket'.
        radius_meters: Search radius in meters (default is 2000.0 meters).

    Returns:
        A list of nearby places including name, formatted address, and location coordinates.
    """
    try:
        api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        if not api_key:
            return "Error: GOOGLE_MAPS_API_KEY environment variable is not set."

        url = "https://places.googleapis.com/v1/places:searchNearby"
        payload = {
            "includedTypes": [place_type],
            "maxResultCount": 5,
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": float(latitude),
                        "longitude": float(longitude),
                    },
                    "radius": float(radius_meters),
                }
            },
        }

        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
            "User-Agent": "SmartPantryAgent/1.0",
        }

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())

        places = data.get("places", [])
        if not places:
            return f"No nearby places of type '{place_type}' found within {radius_meters}m."

        results = []
        for p in places:
            display_name = p.get("displayName", {}).get("text", "Unknown Name")
            address = p.get("formattedAddress", "N/A")
            loc = p.get("location", {})
            lat = loc.get("latitude")
            lng = loc.get("longitude")

            results.append(f"- **{display_name}**\n  Address: {address}\n  Location: ({lat}, {lng})")

        return f"Nearby {place_type} places:\n\n" + "\n\n".join(results)
    except Exception as e:
        return f"Error calling Places API (New): {e}"


async def generate_pantry_item_image(
    prompt: str,
    tool_context: ToolContext,
) -> str:
    """Generates an image for a pantry item or dish using gemini-3.1-flash-lite-image in the global region, saves it as an artifact, uploads it to Cloud Storage, and returns its public HTTPS URL.

    Args:
        prompt: Description of the pantry item or dish image to generate (e.g., 'Fresh red strawberries in a glass bowl').
        tool_context: The ADK ToolContext injected automatically by the framework.

    Returns:
        The public HTTPS URL of the uploaded image in Cloud Storage.
    """
    try:
        genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")
        response = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )

        part = response.candidates[0].content.parts[0]
        image_bytes = part.inline_data.data
        mime_type = part.inline_data.mime_type or "image/jpeg"

        filename = f"pantry_item_{int(time.time())}.jpeg"

        # 1. Save artifact so it appears in Playground Artifacts panel
        artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        await tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload image bytes directly to GCS bucket
        storage_client = storage.Client(project=PROJECT_ID)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
        return public_url
    except Exception as e:
        return f"Error generating or uploading image: {e}"


async def generate_pantry_item_video(
    prompt: str,
    tool_context: ToolContext,
) -> str:
    """Generates a short video for a pantry item or dish using gemini-omni-flash-preview in the global region, saves it as an artifact, uploads it to Cloud Storage, and returns its public HTTPS URL.

    Args:
        prompt: Description of the pantry item or dish video to generate (e.g., 'Fresh sizzling bacon in a hot frying pan').
        tool_context: The ADK ToolContext injected automatically by the framework.

    Returns:
        The public HTTPS URL of the uploaded video in Cloud Storage.
    """
    try:
        genai_client = genai.Client(vertexai=True, project=PROJECT_ID, location="global")
        interaction = genai_client.interactions.create(
            model="gemini-omni-flash-preview",
            input=prompt,
            generation_config={"response_modalities": ["VIDEO"]},
        )

        video_obj = interaction.output_video
        if not video_obj:
            return "Error: No video generated by the model."

        mime_type = getattr(video_obj, "mime_type", "video/mp4") or "video/mp4"
        raw_data = video_obj.data
        if isinstance(raw_data, str):
            video_bytes = base64.b64decode(raw_data)
        else:
            video_bytes = bytes(raw_data)

        filename = f"pantry_item_video_{int(time.time())}.mp4"

        # 1. Save artifact so it appears in Playground Artifacts panel
        artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        await tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload video bytes directly to GCS bucket
        storage_client = storage.Client(project=PROJECT_ID)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"
        return public_url
    except Exception as e:
        return f"Error generating or uploading video: {e}"


# Build A2UI v0.8 System Prompt
schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are the Smart Pantry & Recipe Concierge. You help users manage their pantry inventory "
        "stored in Firestore, find recipes, locate nearby grocery stores, convert addresses into coordinates, "
        "generate item images, execute Python code in a secure sandbox for inventory calculations, "
        "and remember user preferences and facts across conversations."
    ),
    workflow_description="Analyze the user's pantry or recipe request and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        "{\"Image\": {\"url\": {\"literalString\": \"https://...\"}}}. Never point an "
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects.\n\n"
        "CRITICAL FOR USER SAFETY:\n"
        "Pay strict attention to user dietary restrictions and allergies (e.g., peanuts, tree nuts, dairy, gluten, "
        "shellfish, eggs, soy, penicillin, etc.). Always remember all stated user allergies from past conversations, "
        "check ingredients against user allergies, and explicitly warn or filter out any recipe or pantry item "
        "containing a user's allergen."
    ),
    include_schema=True,
    include_examples=True,
)


root_agent = Agent(
    name="smart_pantry_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    code_executor=code_executor,
    instruction=instruction,
    tools=[
        PreloadMemoryTool(),
        list_pantry_items,
        add_or_update_pantry_item,
        find_expiring_items,
        search_external_recipes,
        geocode_address,
        find_nearby_places,
        generate_pantry_item_image,
        generate_pantry_item_video,
    ],
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
)

app = App(
    root_agent=root_agent,
    name="app",
)
