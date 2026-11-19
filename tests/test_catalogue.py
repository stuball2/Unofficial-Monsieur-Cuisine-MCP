import copy

import pytest

from monsieur_cuisine_mcp import catalogue
from monsieur_cuisine_mcp.config import MC_API_BASE_URL

LIST_URL = f"{MC_API_BASE_URL}/api/v2/recipes/page/1/size/100"


@pytest.mark.parametrize("raw,expected", [
    ("Cherry tomatoes (ripe), halved", "cherry tomato"),
    ("Carrots", "carrot"),
    ("Potatoes", "potato"),
    ("Berries", "berry"),
    ("Peaches", "peach"),
    ("asparagus", "asparagus"),
    ("Swiss chard leaves", "swiss chard leave"),
    ("risotto rice", "risotto rice"),
    ("Milk", "milk"),
    (None, ""),
])
def test_normalise(raw, expected):
    assert catalogue.normalise(raw) == expected


def listing(*recipes):
    return {"data": {"total": len(recipes), "totalPage": 1, "currentPage": 1,
                     "recipes": [{"id": r["id"], "name": r["name"], "lastUpdated": r["lastUpdated"]} for r in recipes]}}


def detail_route(mock_http, recipe):
    return mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/{recipe['id']}").respond(json={"data": {"recipe": recipe}})


async def test_sync_stores_recipes_and_ingredients(mock_http, load_fixture):
    recipe = load_fixture("mc3-en-recipe.json")["data"]["recipe"]
    lst = mock_http.get(LIST_URL).respond(json=listing(recipe))
    detail_route(mock_http, recipe)

    result = await catalogue.sync(delay=0)

    assert result["listed"] == 1 and result["fetched"] == 1 and result["errors"] == 0
    req = lst.calls.last.request
    assert req.url.params["filters[deviceTypes][]"] == "MC3.0" and req.headers["device-type"] == "web"
    with catalogue.db() as conn:
        row = conn.execute("SELECT name, servings, categories FROM recipes").fetchone()
        ings = [(r["name"], r["norm"], r["amount"], r["unit"]) for r in conn.execute(
            "SELECT * FROM ingredients ORDER BY position")]
    assert row["name"] == "Test Vanilla Rice Pudding" and row["servings"] == 4
    assert ings[0] == ("Milk", "milk", "400", "g") and ("vanilla extract", "vanilla extract", "0.5", "tsp") in ings
    assert catalogue.stats()["recipes"] == 1


async def test_incremental_sync_only_fetches_changed_and_removes_missing(mock_http, load_fixture):
    a = load_fixture("mc3-en-recipe.json")["data"]["recipe"]
    b = copy.deepcopy(a) | {"id": 800002, "name": "Second"}
    mock_http.get(LIST_URL).respond(json=listing(a, b))
    ra, rb = detail_route(mock_http, a), detail_route(mock_http, b)
    await catalogue.sync(delay=0)
    assert ra.call_count == 1 and rb.call_count == 1

    # Next run: a unchanged, b updated, nothing new; then b disappears.
    b2 = b | {"lastUpdated": "2026-12-01 00:00:00"}
    mock_http.get(LIST_URL).respond(json=listing(a, b2))
    detail_route(mock_http, b2)
    result = await catalogue.sync(delay=0)
    assert result["fetched"] == 1 and ra.call_count == 1

    mock_http.get(LIST_URL).respond(json=listing(a))
    result = await catalogue.sync(delay=0)
    assert result["removed"] == 1 and result["fetched"] == 0
    with catalogue.db() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM recipes")] == [a["id"]]
        assert conn.execute("SELECT COUNT(*) FROM ingredients WHERE recipe_id=800002").fetchone()[0] == 0


async def test_failed_recipe_is_counted_and_sync_continues(mock_http, load_fixture):
    a = load_fixture("mc3-en-recipe.json")["data"]["recipe"]
    b = copy.deepcopy(a) | {"id": 800002}
    mock_http.get(LIST_URL).respond(json=listing(b, a))
    mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/800002").respond(500)
    detail_route(mock_http, a)
    result = await catalogue.sync(delay=0)
    assert result["errors"] == 1 and result["fetched"] == 1
