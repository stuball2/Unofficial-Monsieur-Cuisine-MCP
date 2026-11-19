"""Local SQLite copy of the Monsieur Cuisine Smart recipe catalogue.

Why: the public API can't answer "what can I cook with these ingredients?" and its
listing endpoint carries no ingredients, so each recipe's detail is fetched once
and stored here. Later syncs only re-fetch recipes whose `lastUpdated` changed.

Requests are spaced out (MC_CATALOGUE_REQUEST_DELAY) - a first sync of ~1,000
recipes takes about 17 minutes; nightly syncs usually touch a handful.
"""
import asyncio
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import recipes
from .config import (
    MC_API_DEFAULT_LANGUAGE,
    MC_CATALOGUE_DEVICE,
    MC_CATALOGUE_PATH,
    MC_CATALOGUE_REQUEST_DELAY,
)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY,
    translation_id  INTEGER,
    name            TEXT NOT NULL,
    source          TEXT,            -- official | member
    last_updated    TEXT,
    duration        INTEGER,         -- total minutes
    preparation     INTEGER,
    rating          REAL,
    total_rating    INTEGER,
    complexity      TEXT,
    thumbnail       TEXT,
    categories      TEXT,            -- JSON list of names
    servings        INTEGER,
    serving_unit    TEXT,
    url             TEXT,
    detail_json     TEXT NOT NULL,   -- full get_recipe() "recipe" object
    synced_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingredients (
    recipe_id   INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    name        TEXT NOT NULL,       -- as written in the recipe
    norm        TEXT NOT NULL,       -- normalised for matching (see normalise)
    amount      TEXT,
    unit        TEXT,
    category    TEXT,                -- API ingredientCategory name, e.g. "Vegetables"
    PRIMARY KEY (recipe_id, position)
);
CREATE INDEX IF NOT EXISTS idx_ingredients_norm ON ingredients(norm);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ------------------------------------------------------------------ normalisation
# Bump when normalise() changes: stored `ingredients.norm` values are recomputed on next open.
NORM_VERSION = 2

_PAREN = re.compile(r"\([^)]*\)")
_FRACTIONS = {"¼": " 1/4", "½": " 1/2", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3"}
# Trailing notes that say nothing about what the ingredient is.
_TAIL = re.compile(r"\b(to taste|of your choice|at room temperature|for (the|a|greasing|frying|dusting|decorat\w*|serving|garnish\w*)\b.*"
                   r"|from (a|the) (tin|jar|can|tin/jar)\b.*|in one piece)\b.*$")
_UNITS = ("g|kg|mg|ml|l|cl|dl|tbsp|tsp|cup|cups|pinch|pinches|stalk|stalks|sprig|sprigs|clove|cloves|handful|handfuls"
          "|bunch|bunches|can|cans|tin|tins|slice|slices|piece|pieces|pack|packs|packet|packets|sheet|sheets|knob|dash|drop|drops")
_QUANTITY = re.compile(r"^\s*(?:approx\.?\s*)?[\d.,/\- ]+\s*(?:(?:heaped|level|big|small|large)\s+)?(?:(?:%s)\b\.?\s*)?(?:of\s+)?" % _UNITS)
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_FILLER = {"of", "a", "an", "the", "approx", "untreated", "medium", "large", "small", "big", "sized", "mediumsized",
           "heaped", "level", "about"}
# Kitchen equipment listed among ingredients ("loaf tin", "ovenproof dish", "paper cases").
_EQUIPMENT = {"tin", "dish", "mould", "case", "tray", "skewer", "foil", "film", "ramekin", "cutter", "bag"}
_NO_SINGULAR = {"asparagus", "couscous", "hummus", "molasses", "swiss", "brussels", "lemongrass", "grass",
                "cress", "watercress", "bass", "glass", "series", "species", "oats", "chips"}


_IRREGULAR = {"leaves": "leaf", "halves": "half", "loaves": "loaf", "knives": "knife", "calves": "calf"}


def _singular(word: str) -> str:
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) <= 3 or word in _NO_SINGULAR or word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("oes"):
        return word[:-2]
    if word.endswith(("ches", "shes", "xes", "zes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def normalise(name: str | None) -> str:
    """Reduce an ingredient line to the words that identify it, for matching.

    "450 ml milk (fresh whole milk, 3.8% fat)" -> "milk"
    "2 pinches of pepper" -> "pepper"; "pepper, black, ground" -> "pepper black ground"
    "peppers, red" -> "pepper red"; "butter for the tin" -> "butter"
    "loaf tin (approx. 30 cm long)" -> "" (equipment, not an ingredient)
    Word order isn't meaningful: matching compares word sets.
    """
    if not name:
        return ""
    text = name.lower()
    for frac, rep in _FRACTIONS.items():
        text = text.replace(frac, rep)
    text = _PAREN.sub(" ", text)
    words: list[str] = []
    for part in text.split(","):
        part = _TAIL.sub("", _QUANTITY.sub("", part.strip()))
        words += [w for w in _NON_WORD.sub(" ", part.replace("-", "")).split() if not w.isdigit()]
    words = [_singular(w) for w in words if w not in _FILLER]
    if words and words[-1] in _EQUIPMENT or "paper case" in " ".join(words):
        return ""
    seen: list[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return " ".join(seen)


def renormalise(conn: sqlite3.Connection) -> int:
    """Recompute stored norms after normalise() changed (no re-download needed)."""
    rows = conn.execute("SELECT recipe_id, position, name FROM ingredients").fetchall()
    conn.executemany("UPDATE ingredients SET norm=? WHERE recipe_id=? AND position=?",
                     [(normalise(r["name"]), r["recipe_id"], r["position"]) for r in rows])
    return len(rows)


# ------------------------------------------------------------------ storage
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(MC_CATALOGUE_PATH)


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='norm_version'").fetchone()
    if row is None or json.loads(row["value"]) != NORM_VERSION:
        renormalise(conn)
        set_meta(conn, norm_version=NORM_VERSION)
        conn.commit()
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _default_serving(recipe: dict) -> dict:
    sizes = recipe.get("servingSizes") or []
    for s in sizes:
        if s.get("id") == recipe.get("defaultServingSizeId"):
            return s
    return sizes[0] if sizes else {}


def upsert_recipe(conn: sqlite3.Connection, recipe: dict) -> None:
    serving = _default_serving(recipe)
    thumb = (recipe.get("thumbnail") or {}).get("landscape") or (recipe.get("detailsImage") or {}).get("landscape")
    conn.execute(
        """INSERT OR REPLACE INTO recipes (id, translation_id, name, source, last_updated, duration, preparation,
               rating, total_rating, complexity, thumbnail, categories, servings, serving_unit, url, detail_json, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (recipe["id"], recipe.get("translationId"), (recipe.get("name") or "").strip(), recipe.get("source"),
         recipe.get("lastUpdated"), recipe.get("duration"), recipe.get("preparationDuration"), recipe.get("rating"),
         recipe.get("totalRating"), recipe.get("complexity"), thumb,
         json.dumps([c.get("name") for c in recipe.get("categories") or [] if c.get("name")]),
         serving.get("amount"), serving.get("servingUnit"), recipe.get("url"),
         json.dumps(recipe, ensure_ascii=False), now_iso()))
    conn.execute("DELETE FROM ingredients WHERE recipe_id=?", (recipe["id"],))
    for pos, ing in enumerate(serving.get("ingredients") or []):
        name = (ing.get("name") or "").strip()
        if not name:
            continue
        conn.execute(
            "INSERT INTO ingredients (recipe_id, position, name, norm, amount, unit, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (recipe["id"], pos, name, normalise(name), str(ing.get("amount") or ""), ing.get("unit") or "",
             (ing.get("ingredientCategory") or {}).get("name")))


def set_meta(conn: sqlite3.Connection, **values) -> None:
    for k, v in values.items():
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (k, json.dumps(v)))


def get_meta() -> dict:
    with db() as conn:
        return {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM meta")}


def stats() -> dict:
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        ni = conn.execute("SELECT COUNT(DISTINCT norm) FROM ingredients").fetchone()[0]
    return {"recipes": n, "distinct_ingredients": ni, **get_meta()}


# ------------------------------------------------------------------ sync
class SyncProgress:
    def __init__(self) -> None:
        self.running = False
        self.phase = ""
        self.done = 0
        self.total = 0
        self.errors = 0

    def as_dict(self) -> dict:
        return {"running": self.running, "phase": self.phase, "done": self.done, "total": self.total,
                "errors": self.errors}


progress = SyncProgress()
_lock = asyncio.Lock()


async def sync(delay: float = MC_CATALOGUE_REQUEST_DELAY, language: str = MC_API_DEFAULT_LANGUAGE) -> dict:
    """Bring the local catalogue up to date. Safe to call repeatedly; only one runs at a time."""
    if _lock.locked():
        return {"skipped": "sync already running"}
    async with _lock:
        progress.__init__()
        progress.running = True
        try:
            return await _sync(delay, language)
        finally:
            progress.running = False


async def _sync(delay: float, language: str) -> dict:
    started = now_iso()
    progress.phase = "listing"
    listed: dict[int, str] = {}
    page, total_pages = 1, 1
    while page <= total_pages:
        data = (await recipes.list_recipes_page(page=page, device_type=MC_CATALOGUE_DEVICE, language=language))["data"]
        total_pages = data.get("totalPage") or 1
        for r in data.get("recipes") or []:
            listed[int(r["id"])] = r.get("lastUpdated") or ""
        page += 1
        await asyncio.sleep(delay)

    with db() as conn:
        known = {row["id"]: row["last_updated"] for row in conn.execute("SELECT id, last_updated FROM recipes")}
        removed = [rid for rid in known if rid not in listed]
        for rid in removed:
            conn.execute("DELETE FROM recipes WHERE id=?", (rid,))
    todo = [rid for rid, updated in listed.items() if known.get(rid) != updated]

    progress.phase, progress.total = "fetching", len(todo)
    fetched = 0
    for rid in todo:
        try:
            recipe = (await recipes.get_recipe(str(rid), language=language))["data"]["recipe"]
            with db() as conn:
                upsert_recipe(conn, recipe)
            fetched += 1
        except (httpx.HTTPError, recipes.MCRequestError, KeyError, TypeError) as e:
            progress.errors += 1
            logger.warning("Catalogue: recipe %s failed: %r", rid, e)
        progress.done += 1
        await asyncio.sleep(delay)

    result = {"started": started, "finished": now_iso(), "listed": len(listed), "fetched": fetched,
              "removed": len(removed), "errors": progress.errors}
    with db() as conn:
        set_meta(conn, last_sync=result)
    progress.phase = "done"
    logger.info("Catalogue sync: %s", result)
    return result
