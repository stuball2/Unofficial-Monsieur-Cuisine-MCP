import base64
import copy
import json
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from monsieur_cuisine_mcp.config import MC_API_BASE_URL, MC_PROXY_API_URL
from monsieur_cuisine_mcp.web import app as web
from monsieur_cuisine_mcp.web import recipe_pages as rp

SECRET = "correct-horse-battery"
AUTH = {"Authorization": "Basic " + base64.b64encode(f"stu:{SECRET}".encode()).decode()}
SEARCH_URL = f"{MC_API_BASE_URL}/api/v1/recipes/search/page/1"
FILTERS_URL = f"{MC_API_BASE_URL}/api/v1/recipes/filters"
RECIPE_URL = f"{MC_API_BASE_URL}/api/v2/recipes/800001"
FILTERS = {"code": 0, "data": {"filters": [
    {"identifier": "category", "name": "Category",
     "options": [{"id": 31, "name": "Desserts"}, {"id": 33, "name": "Soups & <Stews>"}]},
    {"identifier": "complexity", "options": [{"id": "Easy", "name": "Easy"}]},
]}}


@pytest.fixture
def client():
    with TestClient(web.create_app(SECRET, with_mcp=False, background=False), follow_redirects=False) as c:
        yield c


def proxy_body(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ------------------------------------------------------------------ pure helpers
def test_format_device_setting():
    assert rp.format_device_setting(None) == ""
    assert rp.format_device_setting({"mode": "customized", "reverse": True, "turbo": False, "settings": [
        {"speed": 1, "temperature": 90, "time": 1800, "weight": None}]}) == "Manual · 90 °C · speed 1 · 30:00 · reverse"
    assert rp.format_device_setting({"mode": "scale", "settings": [
        {"speed": None, "temperature": 0, "time": None, "weight": 400}]}) == "Weigh · 400 g"
    assert rp.format_device_setting({"mode": "kneading", "turbo": True, "settings": [
        {"speed": 3, "time": 65}]}) == "Kneading · speed 3 · 1:05 · turbo"
    assert rp.format_device_setting({"mode": "steam_cooking", "settings": []}) == "Steam Cooking"


def test_strip_html_and_default_serving():
    assert rp.strip_html("<p>Serve <b>warm</b> &amp; enjoy.</p>") == "Serve warm & enjoy."
    assert rp.strip_html(None) == ""
    r = {"defaultServingSizeId": 2, "servingSizes": [{"id": 1}, {"id": 2}]}
    assert rp.default_serving(r) == {"id": 2}
    assert rp.default_serving({"defaultServingSizeId": 9, "servingSizes": [{"id": 1}]}) == {"id": 1}
    assert rp.default_serving({}) == {}


def test_category_options_tolerates_unknown_shapes():
    assert rp._category_options(FILTERS) == [("31", "Desserts"), ("33", "Soups & <Stews>")]
    assert rp._category_options({"data": {"filters": [{"identifier": "category", "values": [{"value": "x"}]}]}}) \
        == [("x", "x")]
    assert rp._category_options({}) == []


# ------------------------------------------------------------------ search
def test_search_renders_results_and_sends_filters(client, mock_http, load_fixture):
    mock_http.get(FILTERS_URL).respond(json=FILTERS)
    route = mock_http.get(SEARCH_URL).respond(json=load_fixture("mc3-en-search-page.json"))
    r = client.get("/recipes", params={"q": "pudding", "category": "31", "max_duration": "45"}, headers=AUTH)
    assert r.status_code == 200
    assert "Test Vanilla Rice Pudding" in r.text and "Test Tomato Soup" in r.text
    assert 'href="/recipes/800001"' in r.text and "pudding-landscape.jpg" in r.text
    assert "2 recipes found" in r.text and "40 min" in r.text and "★ 4.6" in r.text
    assert '<option value="31" selected>Desserts</option>' in r.text
    assert "Soups &amp; &lt;Stews&gt;" in r.text
    assert 'href="/favourites"' in r.text
    params = route.calls.last.request.url.params
    assert params["q"] == "pudding" and params["filters[deviceType]"] == "MC3.0"
    assert params.get_list("filters[category][]") == ["31"] and params["filters[duration][to]"] == "45"
    assert route.calls.last.request.headers["Accept-Language"] == "en-US"


def test_search_paging_links(client, mock_http, load_fixture):
    page = load_fixture("mc3-en-search-page.json")
    page["data"].update(currentPage=2, totalPage=3, total=30)
    mock_http.get(FILTERS_URL).respond(json=FILTERS)
    mock_http.get(f"{MC_API_BASE_URL}/api/v1/recipes/search/page/2").respond(json=page)
    r = client.get("/recipes?q=soup&page=2", headers=AUTH)
    assert "Page 2 of 3" in r.text and "30 recipes found" in r.text
    assert "/recipes?q=soup&amp;page=1" in r.text and "/recipes?q=soup&amp;page=3" in r.text


def test_search_survives_api_errors(client, mock_http):
    mock_http.get(FILTERS_URL).respond(500)
    mock_http.get(SEARCH_URL).respond(503)
    r = client.get("/recipes", headers=AUTH)
    assert r.status_code == 200 and "Search failed" in r.text and "Traceback" not in r.text


def test_search_escapes_untrusted_names(client, mock_http, load_fixture):
    page = load_fixture("mc3-en-search-page.json")
    page["data"]["recipes"][0]["name"] = "<script>alert(1)</script>"
    mock_http.get(FILTERS_URL).respond(json=FILTERS)
    mock_http.get(SEARCH_URL).respond(json=page)
    r = client.get("/recipes", headers=AUTH)
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_recipe_pages_require_auth(client):
    assert client.get("/recipes").status_code == 401
    assert client.get("/favourites").status_code == 401


# ------------------------------------------------------------------ detail
def test_detail_renders_ingredients_and_settings(client, mock_http, load_fixture):
    mock_http.get(RECIPE_URL).respond(json=load_fixture("mc3-en-recipe.json"))
    r = client.get("/recipes/800001", headers=AUTH)
    assert r.status_code == 200
    t = r.text
    assert "Test Vanilla Rice Pudding" in t and "pudding-details.jpg" in t
    assert "<li>400 g Milk</li>" in t and "<li>100 g risotto rice</li>" in t and "<li>1 pinch salt</li>" in t
    assert "4 portions" in t and "5 min" in t and "40 min" in t and "Desserts, Vegetarian" in t
    assert "Serve warm." in t and "<b>warm</b>" not in t
    assert '<div class="settings">Weigh · 400 g</div>' in t
    assert '<div class="settings">Manual · 90 °C · speed 1 · 30:00 · reverse</div>' in t
    assert t.count('class="settings"') == 2  # the text-only "Serve" step has none
    serve = t[t.index("<strong>Serve</strong>"):]
    assert serve.index("Leave to stand") < serve.index("</li>") and 'class="settings"' not in serve[:serve.index("</li>")]
    assert t.index("Pour milk") < t.index("<strong>Cook</strong>") < t.index("<strong>Serve</strong>")
    assert "https://www.monsieur-cuisine.com/en/recipes/test-vanilla-rice-pudding" in t
    assert 'name="translation_id" value="700001"' in t and "Add to favourites" in t and "Remove from favourites" in t


def test_detail_groups_ingredients(client, mock_http, load_fixture):
    data = load_fixture("mc3-en-recipe.json")
    size = data["data"]["recipe"]["servingSizes"][0]
    size["ingredientGroups"] = [{"id": 5, "name": "For the topping"}]
    size["ingredients"][2]["ingredientGroupId"] = 5
    mock_http.get(RECIPE_URL).respond(json=data)
    t = client.get("/recipes/800001", headers=AUTH).text
    assert "For the topping</h3><ul><li>15 g sugar</li></ul>" in t
    assert "<li>400 g Milk</li>" in t


def test_detail_escapes_script_in_name_and_description(client, mock_http, load_fixture):
    data = load_fixture("mc3-en-recipe.json")
    rec = data["data"]["recipe"]
    rec["name"] = "Pud <script>alert('x')</script>"
    rec["description"] = "<img src=x onerror=alert(1)>"
    rec["servingSizes"][0]["steps"][0]["name"] = "<script>step</script>"
    mock_http.get(RECIPE_URL).respond(json=data)
    t = client.get("/recipes/800001", headers=AUTH).text
    assert "<script>" not in t
    assert "Pud &lt;script&gt;" in t and "&lt;img src=x" in t and "&lt;script&gt;step" in t


def test_detail_api_error_is_friendly(client, mock_http, logged_out):
    mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/5").respond(404)
    r = client.get("/recipes/5", headers=AUTH)
    assert r.status_code == 200 and "Could not load this recipe" in r.text


# ------------------------------------------------------------------ favourites
def test_favourites_logged_out(client, logged_out):
    r = client.get("/favourites", headers=AUTH)
    assert r.status_code == 200 and "Not logged in" in r.text and 'href="/"' in r.text


def test_favourites_session_rejected(client, mock_http, logged_in):
    mock_http.post(MC_PROXY_API_URL).respond(403, text="<html>denied</html>")
    r = client.get("/favourites", headers=AUTH)
    assert r.status_code == 200 and "session was rejected" in r.text and 'href="/"' in r.text


def test_favourites_logged_in_with_bookmarks(client, mock_http, logged_in, load_fixture):
    recipe = load_fixture("mc3-en-search-page.json")["data"]["recipes"][0]
    nested = copy.deepcopy(recipe) | {"id": 800002, "name": "Nested <b>Soup</b>"}
    route = mock_http.post(MC_PROXY_API_URL).respond(json={"code": 0, "data": {
        "total": 3, "bookmarks": [recipe, {"translationId": 1, "recipe": nested}, {"unexpected": True}, "junk"]}})
    r = client.get("/favourites", headers=AUTH)
    assert r.status_code == 200
    assert "Test Vanilla Rice Pudding" in r.text and 'href="/recipes/800001"' in r.text
    assert "Nested &lt;b&gt;Soup&lt;/b&gt;" in r.text and 'href="/recipes/800002"' in r.text
    assert "3 saved" in r.text
    assert proxy_body(route)["endpoint"].startswith("api/v2/bookmarks/recipe/details/page/1/")


def test_add_favourite_posts_translation_id(client, mock_http, logged_in):
    route = mock_http.post(MC_PROXY_API_URL).respond(json={"code": 0, "message": "ok"})
    r = client.post("/recipes/800001/favourite", data={"action": "add", "translation_id": "700001"}, headers=AUTH)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    assert loc.path == "/recipes/800001" and parse_qs(loc.query)["msg"] == ["Added to favourites."]
    body = proxy_body(route)
    assert body["method"] == "POST" and body["endpoint"] == "api/v2/bookmarks/recipe"
    assert body["payload"] == {"bookmarkItem": [{"translationId": 700001}]}


def test_remove_favourite_looks_up_translation_id(client, mock_http, logged_in, load_fixture):
    mock_http.get(RECIPE_URL).respond(json=load_fixture("mc3-en-recipe.json"))
    route = mock_http.post(MC_PROXY_API_URL).respond(json={"code": 0})
    r = client.post("/recipes/800001/favourite", data={"action": "remove"}, headers=AUTH)
    assert parse_qs(urlparse(r.headers["location"]).query)["msg"] == ["Removed from favourites."]
    body = proxy_body(route)
    assert body["method"] == "PUT" and body["payload"]["bookmarkItem"][0]["translationId"] == 700001


def test_add_favourite_logged_out_redirects_with_message(client, logged_out):
    r = client.post("/recipes/800001/favourite", data={"action": "add", "translation_id": "700001"}, headers=AUTH)
    assert r.status_code == 303 and "Not+logged+in" in r.headers["location"]


def test_detail_shows_result_message_escaped(client, mock_http, load_fixture):
    mock_http.get(RECIPE_URL).respond(json=load_fixture("mc3-en-recipe.json"))
    t = client.get("/recipes/800001?msg=<i>Added</i>", headers=AUTH).text
    assert "&lt;i&gt;Added&lt;/i&gt;" in t


def test_status_page_has_recipe_nav(client):
    t = client.get("/", headers=AUTH).text
    assert 'href="/recipes"' in t and 'href="/favourites"' in t
