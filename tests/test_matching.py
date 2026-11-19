"""'What can I cook?': matching.find_recipes, the /cook page and the MCP tool.

The catalogue is built with catalogue.upsert_recipe (never synced); conftest points
it at a temp file.
"""
import base64
import copy

import pytest
from starlette.testclient import TestClient

from monsieur_cuisine_mcp import catalogue, matching, server
from monsieur_cuisine_mcp.web import app as web
from monsieur_cuisine_mcp.web import recipe_pages as rp

SECRET = "correct-horse-battery"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"stu:{SECRET}".encode()).decode()}


@pytest.fixture
def base_recipe(load_fixture):
    return load_fixture("mc3-en-recipe.json")["data"]["recipe"]


@pytest.fixture
def add(base_recipe):
    """add(id, name, [ingredient names], rating=..., thumbnail=...) -> stores a recipe."""
    def _add(rid: int, name: str, ingredients: list[str], rating: float | None = 4.0, thumbnail: str | None = None):
        r = copy.deepcopy(base_recipe)
        r.update(id=rid, translationId=rid + 1000, name=name, rating=rating)
        if thumbnail:
            r["thumbnail"]["landscape"] = thumbnail
        template = r["servingSizes"][0]["ingredients"][0]
        r["servingSizes"][0]["ingredients"] = [dict(template, order=i, name=n) for i, n in enumerate(ingredients)]
        with catalogue.db() as conn:
            catalogue.upsert_recipe(conn, r)
        return r
    return _add


def names(results):
    return [r["name"] for r in results]


# ------------------------------------------------------------------ matching rule
@pytest.mark.parametrize("have,ingredient,expected", [
    ("tomato", "Cherry tomatoes", True),
    ("cherry tomato", "tomato", True),
    ("chicken breast", "chicken", True),
    ("Carrots", "carrot", True),
    ("potatoes", "Potato", True),
    ("berries", "berry", True),
    ("pea", "peach", False),
    ("pea", "peanut", False),
    ("peas", "Peaches", False),
    ("pea", "peas", True),
    ("red onion", "spring onion", False),
    ("", "salt", False),
])
def test_ingredient_matches(have, ingredient, expected):
    assert matching.ingredient_matches(have, ingredient) is expected


# ------------------------------------------------------------------ find_recipes
def test_word_boundary_matching_in_catalogue(add):
    add(1, "Tomato salad", ["Cherry tomatoes", "basil"])
    add(2, "Peach crumble", ["Peaches", "flour"])
    add(3, "Peanut sauce", ["peanut butter", "soy sauce"])
    assert names(matching.find_recipes(["tomato"], max_missing=None)) == ["Tomato salad"]
    assert matching.find_recipes(["pea"], max_missing=None) == []


def test_plurals_and_case_are_normalised(add):
    add(1, "Carrot soup", ["carrot", "Onions"])
    res = matching.find_recipes(["Carrots", "onion"])
    assert names(res) == ["Carrot soup"]
    assert res[0]["used"] == ["carrot", "Onions"] and res[0]["missing"] == []


def test_result_shape(add, base_recipe):
    add(800001, "Test Vanilla Rice Pudding", ["Milk", "risotto rice", "sugar", "salt", "vanilla extract"],
        rating=4.6)
    [r] = matching.find_recipes(["milk"])
    assert r == {"id": 800001, "name": "Test Vanilla Rice Pudding",
                 "thumbnail": "https://cdn.example.test/pudding-landscape.jpg", "duration": 40, "rating": 4.6,
                 "servings": 4, "used": ["Milk"], "missing": ["risotto rice", "vanilla extract"],
                 "needed_count": 3}


def test_pantry_staples_never_missing(add):
    add(1, "Fried egg", ["eggs", "olive oil", "Salt", "black pepper", "Water"])
    [r] = matching.find_recipes(["egg"], max_missing=0)
    assert r["missing"] == [] and r["needed_count"] == 1
    # Custom pantry replaces the defaults; an empty pantry means nothing is assumed.
    # Pantry matching is strict: "oil" covers oil, not "olive oil".
    [r] = matching.find_recipes(["egg"], pantry=["oil"], max_missing=None)
    assert r["missing"] == ["olive oil", "Salt", "black pepper", "Water"]
    [r] = matching.find_recipes(["egg"], pantry=[], max_missing=None)
    assert r["needed_count"] == 5


def test_only_pantry_matches_do_not_count_as_used(add):
    add(1, "Salty water", ["salt", "water"])
    assert matching.find_recipes(["salt"], max_missing=None) == []


def test_max_missing_filter(add):
    add(1, "Two missing", ["chicken", "leek", "cream"])
    add(2, "Four missing", ["chicken", "apple", "bean", "carrot", "dill"])
    assert names(matching.find_recipes(["chicken"], max_missing=1)) == []
    assert names(matching.find_recipes(["chicken"], max_missing=2)) == ["Two missing"]
    assert names(matching.find_recipes(["chicken"])) == ["Two missing"]  # default 3
    assert names(matching.find_recipes(["chicken"], max_missing=None)) == ["Two missing", "Four missing"]


def test_recipes_without_my_ingredients_excluded(add):
    add(1, "Rice pudding", ["milk", "rice"])
    add(2, "Beef stew", ["beef", "carrot"])
    assert names(matching.find_recipes(["rice", "banana"], max_missing=None)) == ["Rice pudding"]


def test_ranking_order(add):
    add(1, "B one missing small", ["chicken", "leek"], rating=5.0)          # 1 missing, 50 %
    add(2, "A one missing big", ["chicken", "rice", "leek"], rating=3.0)    # 1 missing, 67 %
    add(3, "Complete low rating", ["chicken", "rice"], rating=2.0)          # 0 missing
    add(4, "Complete high rating", ["chicken"], rating=4.5)                 # 0 missing
    add(5, "Complete no rating", ["rice"], rating=None)                     # 0 missing
    add(6, "Alpha same", ["chicken", "rice", "cream"], rating=3.0)         # 1 missing, 67 %, tie -> name
    assert names(matching.find_recipes(["chicken", "rice"], max_missing=None)) == [
        "Complete high rating", "Complete low rating", "Complete no rating",
        "A one missing big", "Alpha same", "B one missing small"]


def test_limit_and_empty_inputs(add):
    for i in range(5):
        add(i + 1, f"Recipe {i}", ["chicken"])
    assert len(matching.find_recipes(["chicken"], limit=2)) == 2
    assert matching.find_recipes([]) == [] and matching.find_recipes(["  ", ""]) == []


def test_empty_catalogue():
    assert matching.find_recipes(["chicken"]) == []


# ------------------------------------------------------------------ /cook page
@pytest.fixture
def client():
    with TestClient(web.create_app(SECRET, with_mcp=False, background=False), follow_redirects=False) as c:
        yield c


def test_split_ingredients():
    assert rp.split_ingredients("Carrots, onion\n\n chicken breast ,") == ["Carrots", "onion", "chicken breast"]
    assert rp.split_ingredients(None) == []


def test_cook_page_empty_catalogue(client):
    r = client.get("/cook", params={"ingredients": "chicken"}, headers=AUTH)
    assert r.status_code == 200
    assert "hasn't synced yet" in r.text and 'href="/"' in r.text


def test_cook_page_form_defaults(client, add):
    add(1, "Soup", ["leek"])
    r = client.get("/cook", headers=AUTH)
    assert r.status_code == 200
    assert '<option value="3" selected>3</option>' in r.text and '<option value="any">any</option>' in r.text
    assert 'value="water, salt, pepper, black pepper, sugar, oil, olive oil' in r.text
    assert "<textarea" in r.text and "1 recipes in the local catalogue" in r.text
    assert 'href="/cook"' in r.text  # nav link


def test_cook_page_renders_results(client, add):
    add(1, "Tomato salad", ["Cherry tomatoes", "basil", "salt"],
        thumbnail="https://mc-web-user-content-cdn.tecpal.com/x/salad.jpg")
    add(2, "Beef stew", ["beef", "carrot"])
    r = client.get("/cook", params={"ingredients": "Tomatoes\nmozzarella", "max_missing": "1"}, headers=AUTH)
    assert r.status_code == 200
    assert "Tomato salad" in r.text and "Beef stew" not in r.text
    assert 'href="/recipes/1"' in r.text
    assert "<strong>Uses:</strong> Cherry tomatoes" in r.text and "<strong>Missing:</strong> basil" in r.text
    assert "https://mc-recipe-user-prod.b-cdn.net/x/salad.jpg" in r.text
    assert '<option value="1" selected>1</option>' in r.text and "1 recipe found" in r.text


def test_cook_page_max_missing_and_pantry_params(client, add):
    add(1, "Salad", ["tomato", "basil", "salt"])
    r = client.get("/cook", params={"ingredients": "tomato", "max_missing": "0"}, headers=AUTH)
    assert "Salad" not in r.text and "No matching recipes" in r.text
    r = client.get("/cook", params={"ingredients": "tomato", "max_missing": "any", "pantry": ""}, headers=AUTH)
    assert "<strong>Missing:</strong> basil, salt" in r.text
    r = client.get("/cook", params={"ingredients": "tomato", "max_missing": "bogus"}, headers=AUTH)
    assert '<option value="3" selected>3</option>' in r.text and "Salad" in r.text


def test_cook_page_escapes_everything(client, add):
    add(1, '<script>alert("x")</script> pie', ["apple", "<b>cream</b>"])
    r = client.get("/cook", params={"ingredients": 'apple, <img src=x onerror=alert(1)>'}, headers=AUTH)
    assert r.status_code == 200
    assert "<script>alert" not in r.text and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; pie" in r.text
    assert "<b>cream</b>" not in r.text and "&lt;b&gt;cream&lt;/b&gt;" in r.text
    assert "<img src=x" not in r.text


def test_cook_page_requires_auth(client):
    assert client.get("/cook").status_code == 401


# ------------------------------------------------------------------ MCP tool
async def test_mcp_tool_matches_find_recipes(add):
    add(1, "Tomato salad", ["Cherry tomatoes", "basil", "salt"])
    add(2, "Tomato soup", ["tomato", "onion", "stock", "cream"])
    add(3, "Peach crumble", ["peach", "flour"])
    expected = matching.find_recipes(["tomato"], max_missing=3, limit=20)
    assert names(expected) == ["Tomato salad", "Tomato soup"]
    assert await server.find_recipes_by_ingredients(["tomato"]) == expected
    assert await server.find_recipes_by_ingredients(["tomato"], max_missing=1, limit=5) == \
        matching.find_recipes(["tomato"], max_missing=1, limit=5)


async def test_mcp_tool_is_registered():
    tool = await server.mcp.get_tool("find_recipes_by_ingredients")
    assert tool is not None and "local copy" in tool.description and "en-US" in tool.description


def test_pantry_pepper_does_not_hide_red_peppers(add):
    add(1, "Stuffed peppers", ["1 red pepper (200 g)", "peppers, yellow", "pepper, black, ground",
                               "2 pinches of pepper", "salt to taste", "rice", "cayenne pepper, ground"])
    [r] = matching.find_recipes(["rice"], max_missing=None)
    assert r["missing"] == ["1 red pepper (200 g)", "peppers, yellow", "cayenne pepper, ground"]


def test_pantry_water_does_not_hide_coconut_water(add):
    add(1, "Smoothie", ["banana", "coconut water", "cold water"])
    [r] = matching.find_recipes(["banana"], max_missing=None)
    assert r["missing"] == ["coconut water"]


def test_equipment_lines_are_ignored(add):
    add(1, "Loaf", ["flour", "loaf tin (approx. 30 cm long)", "butter for the tin"])
    [r] = matching.find_recipes(["flour", "butter"], max_missing=0)
    assert r["needed_count"] == 2
